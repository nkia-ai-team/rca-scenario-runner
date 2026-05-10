#!/usr/bin/env bash
# =============================================================================
# 시나리오 02: Courier Pool Exhaustion (배달원 부족 capacity 한계)
# =============================================================================
# 도메인 배경: 음식 배달 서비스의 핵심 capacity constraint = 배달원 수.
#              점심·저녁 피크 타임에 가용 courier 가 모두 배차 (ASSIGNED) 상태가
#              되면 신규 주문은 dispatch 할당을 받지 못함. dispatch-service 가
#              "가용 courier 없음" 응답을 반복하거나 매우 큰 ETA 를 부여 →
#              사용자 경험 악화 + DB 의 dispatches 테이블 row 폭증.
#
# Root Cause: dispatches 테이블에 가상의 ASSIGNED courier 200건 pre-seed →
#             courier pool 이 가득찬 상태에서 동시 주문 → dispatch-service 가
#             신규 dispatch row insert 시 ETA 무한 증가 또는 fallback 처리.
#
# 전파 경로: courier pool 포화 → POST /api/orders 의 dispatch fan-out 호출 시
#            응답 지연 (ETA 계산 + DB 조회 race) → order 응답 시간 증가 →
#            dispatches 테이블 DML 부하 → DB row 폭증
#
# food-delivery 도메인 unique: plopvape 의 e-commerce 도메인엔 "배달 capacity"
#   개념 없음. 본 시나리오는 배달 서비스의 핵심 제약 (courier 수) 을 직접 공격.
#
# 사용법:
#   ./scenario-02-courier-pool-exhaustion.sh         # 실행
#   ./scenario-02-courier-pool-exhaustion.sh cleanup # 원상복구
# =============================================================================

set -uo pipefail

export KUBECONFIG=/home/nkia/.kube/food-delivery.yaml
NAMESPACE="rca-testbed-food"
DB_POD="testbed-postgres-0"
DB_USER="fooddelivery"
DB_PASS="fooddelivery1234"
DB_NAME="fooddelivery"
ORDER_POD_LABEL="app=testbed-order"
DISPATCH_POD_LABEL="app=testbed-dispatch"
COURIER_POOL_SIZE=200       # 가상 courier pre-seed 수
EXHAUST_DURATION=240
CONCURRENT_ORDERS=20
ORDER_ROUNDS=5
SEED_ORDER_BASE_ID=900000   # cleanup 식별용 큰 id 영역

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
    local op; op=$(order_pod)
    [[ -z "$op" ]] && { log_error "order-service pod 없음"; exit 1; }
    if ! kubectl -n "$NAMESPACE" get pod "$DB_POD" &>/dev/null; then
        log_error "postgres pod 없음"; exit 1
    fi
    log_ok "사전 조건 OK"
}

seed_courier_pool() {
    log_info "=== courier pool exhaust pre-seed: ${COURIER_POOL_SIZE} ASSIGNED dispatches 생성 ==="
    # 가상 order_id 들 (FK constraint 회피 위해 실재 order 먼저 1개 생성)
    psql_exec -c "
        INSERT INTO orders (id, customer_id, restaurant_id, total_amount, status)
        SELECT g, 'seed-pool-' || g, 1, 0.0, 'PENDING'
        FROM generate_series($SEED_ORDER_BASE_ID, $SEED_ORDER_BASE_ID + $COURIER_POOL_SIZE - 1) g
        ON CONFLICT (id) DO NOTHING;
    " 2>/dev/null
    psql_exec -c "
        INSERT INTO dispatches (order_id, courier_id, eta_minutes, status)
        SELECT g, 'virtual-courier-' || g, 30, 'ASSIGNED'
        FROM generate_series($SEED_ORDER_BASE_ID, $SEED_ORDER_BASE_ID + $COURIER_POOL_SIZE - 1) g;
    " 2>/dev/null
    local d_count
    d_count=$(psql_exec -t -A -c "SELECT count(*) FROM dispatches WHERE status='ASSIGNED';" 2>/dev/null)
    log_ok "ASSIGNED dispatches 총 $d_count (pool 포화 상태)"
}

send_orders_capacity_test() {
    local round=$1
    log_info "=== Round $round: 포화 상태 동시 주문 (${CONCURRENT_ORDERS}건) ==="
    local op; op=$(order_pod)
    local pids=()
    for i in $(seq 1 "$CONCURRENT_ORDERS"); do
        kubectl -n "$NAMESPACE" exec "$op" -- \
            curl -s -o /dev/null \
            -w "cap-${round}-${i}: HTTP %{http_code} in %{time_total}s\n" \
            --max-time 30 \
            -X POST "http://localhost:8080/api/orders" \
            -H "Content-Type: application/json" \
            -d "{\"customerId\":\"capacity-${round}-${i}\",\"restaurantId\":1,\"items\":[{\"menuId\":1,\"qty\":1}]}" \
            >> /tmp/scenario-02-fd-results.log 2>&1 &
        pids+=($!)
    done
    for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
}

monitor_dispatch_growth() {
    log_info "=== dispatch table growth + ETA distribution ==="
    psql_exec -c "
        SELECT count(*) as total_assigned,
               max(eta_minutes) as max_eta_min,
               avg(eta_minutes)::int as avg_eta_min
        FROM dispatches WHERE status='ASSIGNED';
    " 2>/dev/null || true
}

analyze_results() {
    log_info "=== 결과 분석 ==="
    if [[ -f /tmp/scenario-02-fd-results.log ]]; then
        local total avg_time max_time errors
        total=$(wc -l < /tmp/scenario-02-fd-results.log)
        errors=$(grep -c "HTTP [45]" /tmp/scenario-02-fd-results.log 2>/dev/null || echo "0")
        avg_time=$(awk -F'in ' '{print $2}' /tmp/scenario-02-fd-results.log 2>/dev/null | awk -F's' '{s+=$1; c++} END {if(c>0) printf "%.2f", s/c; else print "N/A"}')
        max_time=$(awk -F'in ' '{print $2}' /tmp/scenario-02-fd-results.log 2>/dev/null | awk -F's' '{if ($1>m) m=$1} END {printf "%.2f", m}')
        echo "======================================"
        echo "  시나리오 02: Courier Pool Exhaustion"
        echo "======================================"
        echo "  총 요청:        $total"
        echo "  HTTP 에러:      $errors"
        echo "  평균 응답시간:  ${avg_time}s (예상: capacity 포화로 증가)"
        echo "  최대 응답시간:  ${max_time}s"
        echo "======================================"
        log_info "기대: dispatch ETA 증가 + APM dispatch-service 응답시간 spike"
    fi
}

cleanup() {
    log_info "=== 원상복구 (courier pool 정리) ==="
    psql_exec -c "DELETE FROM dispatches WHERE order_id BETWEEN $SEED_ORDER_BASE_ID AND $SEED_ORDER_BASE_ID + $COURIER_POOL_SIZE * 2;" 2>/dev/null || true
    psql_exec -c "DELETE FROM orders WHERE id BETWEEN $SEED_ORDER_BASE_ID AND $SEED_ORDER_BASE_ID + $COURIER_POOL_SIZE * 2;" 2>/dev/null || true
    rm -f /tmp/scenario-02-fd-*.log
    log_ok "seed orders/dispatches 정리 완료"
}

main() {
    echo "============================================================"
    echo "  시나리오 02: Courier Pool Exhaustion (delivery domain)"
    echo "  시작: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "============================================================"
    rm -f /tmp/scenario-02-fd-results.log
    trap cleanup EXIT
    check_prerequisites; echo
    seed_courier_pool; echo
    for r in $(seq 1 "$ORDER_ROUNDS"); do
        send_orders_capacity_test "$r"
        monitor_dispatch_growth; echo
        sleep 8
    done
    analyze_results
    log_info "${EXHAUST_DURATION}s 대기 후 cleanup"
    sleep $((EXHAUST_DURATION - ORDER_ROUNDS * 12))
    echo "  시나리오 02 완료"
}

if [[ "${1:-}" == "cleanup" ]]; then cleanup; trap - EXIT; else main; fi
