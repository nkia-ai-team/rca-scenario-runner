#!/usr/bin/env bash
# =============================================================================
# G-9 / food-delivery: Trace Latency Fanout Without Error Spike
# =============================================================================
# Root Cause: dispatch endpoint에 bounded high-latency successful requests를 만들어
#             error spike보다 trace/link latency fanout이 주 증거가 되게 한다.
# =============================================================================

set -uo pipefail

export KUBECONFIG="${KUBECONFIG:-/root/tb-kubeconfig}"
NAMESPACE="rca-testbed-food"
ORDER_POD_LABEL="app=testbed-order"
RESULT_LOG="/tmp/g9-food-trace-latency-fanout.log"
ROUNDS=6
CONCURRENT_CALLS=25

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
log_info()  { echo -e "${CYAN}[INFO]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}   $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }

order_pod() {
    kubectl -n "$NAMESPACE" get pod -l "$ORDER_POD_LABEL" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null
}

check_prerequisites() {
    local pod
    pod=$(order_pod)
    if [[ -z "$pod" ]]; then
        log_error "order-service pod not found"
        exit 1
    fi
}

send_latency_fanout() {
    local round=$1
    local pod
    pod=$(order_pod)
    log_info "Round $round: direct dispatch fanout calls"
    local pids=()
    for i in $(seq 1 "$CONCURRENT_CALLS"); do
        kubectl -n "$NAMESPACE" exec "$pod" -- \
            curl -s -o /dev/null \
            -w "g9-dispatch-${round}-${i}: HTTP %{http_code} in %{time_total}s\n" \
            --max-time 20 \
            -X POST "http://testbed-dispatch:8082/api/deliveries/dispatch" \
            -H "Content-Type: application/json" \
            -d "{\"orderId\":$((700000 + round * 100 + i)),\"region\":\"GANGNAM\"}" \
            >> "$RESULT_LOG" 2>&1 &
        pids+=($!)
    done
    for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
}

cleanup() {
    rm -f "$RESULT_LOG"
    log_info "INJECTION_END_MS=$(date +%s%3N)"
    log_ok "Cleanup complete"
}

main() {
    if [[ "${1:-}" == "cleanup" ]]; then cleanup; trap - EXIT; exit 0; fi
    echo "============================================================"
    echo "  G-9 food-delivery: Trace Latency Fanout"
    echo "  Start: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "============================================================"
    rm -f "$RESULT_LOG"
    trap cleanup EXIT
    check_prerequisites
    log_info "INJECTION_START_MS=$(date +%s%3N)"
    for round in $(seq 1 "$ROUNDS"); do
        send_latency_fanout "$round"
        sleep 8
    done
    [[ -f "$RESULT_LOG" ]] && tail -n 30 "$RESULT_LOG"
}

main "$@"
