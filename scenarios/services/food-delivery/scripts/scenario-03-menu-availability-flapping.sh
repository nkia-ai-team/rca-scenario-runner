#!/usr/bin/env bash
# =============================================================================
# 시나리오 03: Menu Availability Flapping (메뉴 매진 toggle race)
# =============================================================================
# 도메인 배경: 메뉴별 가용성 (menus.available) 이 실시간으로 자주 바뀌는 현실
#              (재료 소진 / 일시 품절 / 가게 자율 토글). 인기 메뉴가 일제히
#              매진되었다가 재입고되는 패턴이 반복되면 in-memory cache 와 DB
#              간 stale → 사용자가 클릭한 시점엔 available 였는데, 결제 직전
#              마지막 검증 단계에 unavailable 로 바뀜 → race condition + retry.
#
# Root Cause: 0.5초 주기로 menus 의 50% 를 random 하게 available=NOT available
#             토글. 동시 POST /api/orders 가 매번 다른 menu set 을 hit →
#             일부는 통과, 일부는 4xx 반환. 비결정적 응답 + retry pattern.
#
# 전파 경로: menus available flap → order POST 시 menu 검증 단계 (item validation)
#            에서 비결정적 4xx → 사용자 retry → 재요청 폭주 → DB 의 menus
#            UPDATE/SELECT 경합 → restaurant-service 도 menu 조회 캐시 stale
#
# food-delivery 도메인 unique: 메뉴 매진/재입고는 음식 배달에서 매우 흔한 패턴.
#   plopvape 의 inventory stock 과 다른 점 — plopvape 는 stock=integer 감소
#   (lock 필요), 본 시나리오는 boolean 토글 (lock 없이도 race 발생).
#
# 사용법:
#   ./scenario-03-menu-availability-flapping.sh         # 실행
#   ./scenario-03-menu-availability-flapping.sh cleanup # 원상복구
# =============================================================================

set -uo pipefail

export KUBECONFIG=/home/nkia/.kube/food-delivery.yaml
NAMESPACE="rca-testbed-food"
DB_POD="testbed-postgres-0"
DB_USER="fooddelivery"
DB_PASS="fooddelivery1234"
DB_NAME="fooddelivery"
ORDER_POD_LABEL="app=testbed-order"
FLAP_DURATION=180
FLAP_INTERVAL_MS=500        # 0.5초 토글 주기
CONCURRENT_ORDERS=15
ORDER_ROUNDS=12
FLAPPER_PID_FILE="/tmp/scenario-03-fd-flapper.pid"

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
    local menu_count
    menu_count=$(psql_exec -t -A -c "SELECT count(*) FROM menus;" 2>/dev/null)
    log_info "menus 테이블 row 수: $menu_count"
    if [[ "$menu_count" -lt 5 ]]; then
        log_error "menus 데이터 부족 (init.sql 확인)"; exit 1
    fi
    psql_exec -c "UPDATE menus SET available=TRUE;" 2>/dev/null
    log_ok "사전 조건 OK + 모든 메뉴 available=true 로 리셋"
}

start_flapper() {
    log_info "=== 메뉴 availability flap 시작 (${FLAP_INTERVAL_MS}ms 간격, random 50% toggle) ==="
    (
        local elapsed_ms=0
        local end_ms=$((FLAP_DURATION * 1000))
        while [ "$elapsed_ms" -lt "$end_ms" ]; do
            kubectl -n "$NAMESPACE" exec "$DB_POD" -- env PGPASSWORD="$DB_PASS" \
                psql -U "$DB_USER" -d "$DB_NAME" \
                -c "UPDATE menus SET available = NOT available WHERE random() < 0.5;" >/dev/null 2>&1
            sleep "$(awk "BEGIN {print $FLAP_INTERVAL_MS / 1000}")"
            elapsed_ms=$((elapsed_ms + FLAP_INTERVAL_MS))
        done
    ) &
    local fp=$!
    echo "$fp" > "$FLAPPER_PID_FILE"
    log_ok "Flapper background PID: $fp"
}

send_orders_random_menu() {
    local round=$1
    log_info "=== Round $round: random menu 주문 (${CONCURRENT_ORDERS}건) ==="
    local op; op=$(order_pod)
    local pids=()
    for i in $(seq 1 "$CONCURRENT_ORDERS"); do
        local rid=$(( (RANDOM % 3) + 1 ))
        local mid=$(( ((rid - 1) * 5) + (RANDOM % 5) + 1 ))
        kubectl -n "$NAMESPACE" exec "$op" -- \
            curl -s -o /dev/null \
            -w "mf-${round}-${i}-menu${mid}: HTTP %{http_code} in %{time_total}s\n" \
            --max-time 20 \
            -X POST "http://localhost:8080/api/orders" \
            -H "Content-Type: application/json" \
            -d "{\"customerId\":\"flap-${round}-${i}\",\"restaurantId\":$rid,\"items\":[{\"menuId\":$mid,\"qty\":1}]}" \
            >> /tmp/scenario-03-fd-results.log 2>&1 &
        pids+=($!)
    done
    for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
}

monitor_menu_state() {
    psql_exec -c "
        SELECT (SELECT count(*) FROM menus WHERE available=TRUE) as available,
               (SELECT count(*) FROM menus WHERE available=FALSE) as soldout;
    " 2>/dev/null || true
}

analyze_results() {
    log_info "=== 결과 분석 ==="
    if [[ -f /tmp/scenario-03-fd-results.log ]]; then
        local total ok2xx err4xx err5xx avg_time
        total=$(wc -l < /tmp/scenario-03-fd-results.log)
        ok2xx=$(grep -c "HTTP 2" /tmp/scenario-03-fd-results.log 2>/dev/null || echo "0")
        err4xx=$(grep -c "HTTP 4" /tmp/scenario-03-fd-results.log 2>/dev/null || echo "0")
        err5xx=$(grep -c "HTTP 5" /tmp/scenario-03-fd-results.log 2>/dev/null || echo "0")
        avg_time=$(awk -F'in ' '{print $2}' /tmp/scenario-03-fd-results.log 2>/dev/null | awk -F's' '{s+=$1; c++} END {if(c>0) printf "%.2f", s/c; else print "N/A"}')
        echo "======================================"
        echo "  시나리오 03: Menu Availability Flapping"
        echo "======================================"
        echo "  총 요청:        $total"
        echo "  2xx 성공:       $ok2xx (random ~50%)"
        echo "  4xx 매진:       $err4xx"
        echo "  5xx 에러:       $err5xx"
        echo "  평균 응답시간:  ${avg_time}s"
        echo "======================================"
        log_info "기대: 2xx/4xx 비결정적 분포 + DB menus UPDATE rate spike"
    fi
}

cleanup() {
    log_info "=== 원상복구 ==="
    if [[ -f "$FLAPPER_PID_FILE" ]]; then
        local fp; fp=$(cat "$FLAPPER_PID_FILE")
        kill -0 "$fp" 2>/dev/null && { kill "$fp" 2>/dev/null; sleep 1; kill -9 "$fp" 2>/dev/null; } || true
        rm -f "$FLAPPER_PID_FILE"
        log_ok "Flapper PID $fp 종료"
    fi
    psql_exec -c "UPDATE menus SET available=TRUE;" 2>/dev/null || true
    rm -f /tmp/scenario-03-fd-*.log
    log_ok "모든 menu available=true 복구"
}

main() {
    echo "============================================================"
    echo "  시나리오 03: Menu Availability Flapping (food domain race)"
    echo "  시작: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "============================================================"
    rm -f /tmp/scenario-03-fd-results.log
    trap cleanup EXIT
    check_prerequisites; echo
    start_flapper; echo
    for r in $(seq 1 "$ORDER_ROUNDS"); do
        send_orders_random_menu "$r"
        log_info "menu 상태:"; monitor_menu_state
        echo
        sleep 3
    done
    analyze_results
    echo "  시나리오 03 완료"
}

if [[ "${1:-}" == "cleanup" ]]; then cleanup; trap - EXIT; else main; fi
