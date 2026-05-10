#!/usr/bin/env bash
# =============================================================================
# 시나리오 01: Restaurant Closed Mid-Order Storm
# =============================================================================
# 도메인 배경: 음식 배달 서비스에서 영업 종료 시점 (예: 23시 마감) 또는 갑작스러운
#              가게 휴업 발생 시 신규 주문이 일제히 거부됨. order-service 는 주문
#              생성 시 restaurant-service 로 식당 status 를 조회해야 하는데, 모든
#              식당이 CLOSED 면 검증 단계에서 거부 (4xx/5xx). 비즈니스 검증 로직의
#              cold path 폭주 + restaurant-service 의 GET /api/restaurants/{id}
#              호출 rate 급증.
#
# Root Cause: restaurants 테이블의 status='CLOSED' UPDATE → 동시 주문 폭주 시
#             order-service 의 restaurant validation 단계 (fan-out 호출) 가
#             모두 "closed restaurant" 응답 받음.
#
# 전파 경로: status='CLOSED' → POST /api/orders → restaurant validation 거부
#            → order 4xx/5xx 응답률 급증 → 사용자 재시도 → restaurant-service
#            GET rate 폭증 → DB 의 restaurants 테이블 SELECT 폭증
#
# plopvape 와의 차이: plopvape 01 (inventory-lock) 은 DB row-lock + 동시
#   INSERT 경합. 본 시나리오는 비즈니스 도메인 검증 실패 — DB lock 없이도
#   business logic level 에서 모든 주문이 거부됨. 다른 failure surface.
#
# 사용법:
#   ./scenario-01-restaurant-closed-storm.sh         # 실행
#   ./scenario-01-restaurant-closed-storm.sh cleanup # 원상복구
# =============================================================================

set -uo pipefail

export KUBECONFIG=/home/nkia/.kube/food-delivery.yaml
NAMESPACE="rca-testbed-food"
DB_POD="testbed-postgres-0"
DB_USER="fooddelivery"
DB_PASS="fooddelivery1234"
DB_NAME="fooddelivery"
ORDER_POD_LABEL="app=testbed-order"
CLOSED_DURATION=180         # 식당 CLOSED 유지 시간
CONCURRENT_ORDERS=30
ORDER_ROUNDS=4

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log_info()  { echo -e "${CYAN}[INFO]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}   $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }

psql_exec() {
    kubectl -n "$NAMESPACE" exec "$DB_POD" -- env PGPASSWORD="$DB_PASS" \
        psql -U "$DB_USER" -d "$DB_NAME" "$@"
}

order_pod() {
    kubectl -n "$NAMESPACE" get pod -l "$ORDER_POD_LABEL" -o jsonpath='{.items[0].metadata.name}'
}

check_prerequisites() {
    log_info "사전 조건 확인 중..."
    local count
    count=$(psql_exec -t -A -c "SELECT count(*) FROM restaurants WHERE status='OPEN';" 2>/dev/null)
    log_info "현재 OPEN 식당 수: $count"
    if [[ "$count" -lt 1 ]]; then
        log_warn "OPEN 식당 0개 — seed 복구 후 진행"
        psql_exec -c "UPDATE restaurants SET status='OPEN';" 2>/dev/null
    fi
    local op; op=$(order_pod)
    [[ -z "$op" ]] && { log_error "order-service pod 없음"; exit 1; }
    log_ok "order pod: $op"
}

close_all_restaurants() {
    log_info "=== 모든 restaurant 일제 CLOSED (영업 종료 시뮬레이션) ==="
    psql_exec -c "UPDATE restaurants SET status='CLOSED';" 2>/dev/null
    psql_exec -c "SELECT id, name, status FROM restaurants ORDER BY id;" 2>/dev/null
    log_ok "전체 restaurant CLOSED 처리"
}

send_blocked_orders() {
    local round=$1
    log_info "=== Round $round: CLOSED 식당 대상 주문 (${CONCURRENT_ORDERS}건) ==="
    local op; op=$(order_pod)
    local pids=()
    for i in $(seq 1 "$CONCURRENT_ORDERS"); do
        local rid=$(( (RANDOM % 3) + 1 ))
        local mid=$(( ((rid - 1) * 5) + (RANDOM % 5) + 1 ))
        kubectl -n "$NAMESPACE" exec "$op" -- \
            curl -s -o /dev/null \
            -w "rc-${round}-${i}: HTTP %{http_code} in %{time_total}s\n" \
            --max-time 30 \
            -X POST "http://localhost:8080/api/orders" \
            -H "Content-Type: application/json" \
            -d "{\"customerId\":\"closed-${round}-${i}\",\"restaurantId\":$rid,\"items\":[{\"menuId\":$mid,\"qty\":1}]}" \
            >> /tmp/scenario-01-fd-results.log 2>&1 &
        pids+=($!)
    done
    for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
}

analyze_results() {
    log_info "=== 결과 분석 ==="
    if [[ -f /tmp/scenario-01-fd-results.log ]]; then
        local total err4xx err5xx ok2xx
        total=$(wc -l < /tmp/scenario-01-fd-results.log)
        err4xx=$(grep -c "HTTP 4" /tmp/scenario-01-fd-results.log 2>/dev/null || echo "0")
        err5xx=$(grep -c "HTTP 5" /tmp/scenario-01-fd-results.log 2>/dev/null || echo "0")
        ok2xx=$(grep -c "HTTP 2" /tmp/scenario-01-fd-results.log 2>/dev/null || echo "0")
        echo "======================================"
        echo "  시나리오 01: Restaurant Closed Storm"
        echo "======================================"
        echo "  총 요청:        $total"
        echo "  2xx 성공:       $ok2xx"
        echo "  4xx 거부:       $err4xx (예상: 대부분 — restaurant validation fail)"
        echo "  5xx 에러:       $err5xx"
        echo "======================================"
        log_info "기대: 4xx >> 2xx (CLOSED 식당 검증 거부로 인한 비즈니스 fail)"
    fi
}

cleanup() {
    log_info "=== 원상복구 (restaurant 영업 재개) ==="
    psql_exec -c "UPDATE restaurants SET status='OPEN';" 2>/dev/null || true
    rm -f /tmp/scenario-01-fd-*.log
    log_ok "전체 restaurant OPEN 복구"
}

main() {
    echo "============================================================"
    echo "  시나리오 01: Restaurant Closed Mid-Order Storm"
    echo "  시작: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "============================================================"
    rm -f /tmp/scenario-01-fd-results.log
    trap cleanup EXIT
    check_prerequisites; echo
    close_all_restaurants; echo
    for r in $(seq 1 "$ORDER_ROUNDS"); do
        send_blocked_orders "$r"; echo
        sleep 5
    done
    analyze_results
    log_info "${CLOSED_DURATION}s 대기 후 cleanup (알람 발화 시간 확보)"
    sleep $((CLOSED_DURATION - ORDER_ROUNDS * 8))
    echo "  시나리오 01 완료"
}

if [[ "${1:-}" == "cleanup" ]]; then cleanup; trap - EXIT; else main; fi
