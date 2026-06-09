#!/usr/bin/env bash
# =============================================================================
# G-1 / food-delivery: External PG Timeout Upstream Edge
# =============================================================================
# Root Cause: food-delivery external-pg-mock을 중단해 payment-service의 외부
#             결제 의존성 호출을 실패/timeout 상태로 만든다.
#
# Expected RCA: external payment gateway timeout/root, not DB lock.
#
# Usage:
#   ./scenario-05-external-pg-timeout-upstream.sh
#   ./scenario-05-external-pg-timeout-upstream.sh cleanup
# =============================================================================

set -uo pipefail

export KUBECONFIG=/home/nkia/.kube/food-delivery.yaml
NAMESPACE="rca-testbed-food"
ORDER_POD_LABEL="app=testbed-order"
PG_MOCK_DEPLOY="testbed-external-pg-mock"
ORIG_REPLICAS_FILE="/tmp/g1-food-external-pg-orig-replicas"
RESULT_LOG="/tmp/g1-food-external-pg-results.log"
CONCURRENT_ORDERS=20
ORDER_ROUNDS=4

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log_info()  { echo -e "${CYAN}[INFO]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}   $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }

order_pod() {
    kubectl -n "$NAMESPACE" get pod -l "$ORDER_POD_LABEL" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null
}

check_prerequisites() {
    log_info "Checking prerequisites"
    local pod
    pod=$(order_pod)
    if [[ -z "$pod" ]]; then
        log_error "order-service pod not found"
        exit 1
    fi
    if ! kubectl -n "$NAMESPACE" get deploy "$PG_MOCK_DEPLOY" &>/dev/null; then
        log_error "deployment not found: $PG_MOCK_DEPLOY"
        exit 1
    fi
    local code
    code=$(kubectl -n "$NAMESPACE" exec "$pod" -- curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
        "http://localhost:8080/actuator/health" 2>/dev/null || echo "000")
    if [[ "$code" != "200" ]]; then
        log_warn "order health returned HTTP $code; continuing because scenario may still be runnable"
    fi
    log_ok "Prerequisites checked"
}

induce_external_timeout() {
    log_info "INJECTION_START_MS=$(date +%s%3N)"
    kubectl -n "$NAMESPACE" get deploy "$PG_MOCK_DEPLOY" -o jsonpath='{.spec.replicas}' > "$ORIG_REPLICAS_FILE"
    kubectl -n "$NAMESPACE" scale deploy "$PG_MOCK_DEPLOY" --replicas=0
    sleep 5
    log_ok "Scaled $PG_MOCK_DEPLOY to 0 replicas"
}

send_orders() {
    local round=$1
    local pod
    pod=$(order_pod)
    log_info "Round $round: sending $CONCURRENT_ORDERS payment-path orders"
    local pids=()
    for i in $(seq 1 "$CONCURRENT_ORDERS"); do
        kubectl -n "$NAMESPACE" exec "$pod" -- \
            curl -s -o /dev/null \
            -w "g1-order-${round}-${i}: HTTP %{http_code} in %{time_total}s\n" \
            --max-time 35 \
            -X POST "http://localhost:8080/api/orders" \
            -H "Content-Type: application/json" \
            -d "{\"customerId\":\"g1-ext-${round}-${i}\",\"restaurantId\":1,\"items\":[{\"menuId\":1,\"qty\":1}]}" \
            >> "$RESULT_LOG" 2>&1 &
        pids+=($!)
    done
    for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
}

analyze_results() {
    log_info "Result summary"
    if [[ ! -f "$RESULT_LOG" ]]; then
        log_warn "No result log"
        return
    fi
    local total errors avg_time max_time slow
    total=$(wc -l < "$RESULT_LOG")
    errors=$(grep -c "HTTP [45]" "$RESULT_LOG" 2>/dev/null || echo "0")
    avg_time=$(awk -F'in ' '{print $2}' "$RESULT_LOG" | awk -F's' '{s+=$1; c++} END {if(c>0) printf "%.2f", s/c; else print "N/A"}')
    max_time=$(awk -F'in ' '{print $2}' "$RESULT_LOG" | awk -F's' '{if($1>m)m=$1} END {printf "%.2f", m}')
    slow=$(awk -F'in ' '{print $2}' "$RESULT_LOG" | awk -F's' '{if($1>5)c++} END {print c+0}')
    echo "total=$total errors=$errors slow_gt_5s=$slow avg=${avg_time}s max=${max_time}s"
    tail -n 20 "$RESULT_LOG"
}

cleanup() {
    log_info "Cleanup start"
    local orig=1
    if [[ -f "$ORIG_REPLICAS_FILE" ]]; then
        orig=$(cat "$ORIG_REPLICAS_FILE")
    fi
    kubectl -n "$NAMESPACE" scale deploy "$PG_MOCK_DEPLOY" --replicas="$orig" 2>/dev/null || true
    rm -f "$ORIG_REPLICAS_FILE" "$RESULT_LOG"
    log_info "INJECTION_END_MS=$(date +%s%3N)"
    log_ok "Cleanup complete (replicas=$orig)"
}

main() {
    if [[ "${1:-}" == "cleanup" ]]; then
        cleanup
        trap - EXIT
        exit 0
    fi

    echo "============================================================"
    echo "  G-1 food-delivery: External PG Timeout Upstream Edge"
    echo "  Start: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "============================================================"
    rm -f "$RESULT_LOG"
    trap cleanup EXIT
    check_prerequisites
    induce_external_timeout
    for round in $(seq 1 "$ORDER_ROUNDS"); do
        send_orders "$round"
        sleep 8
    done
    analyze_results
}

main "$@"
