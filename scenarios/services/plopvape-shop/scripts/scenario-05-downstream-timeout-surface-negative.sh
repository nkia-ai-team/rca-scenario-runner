#!/usr/bin/env bash
# =============================================================================
# G-2 / plopvape-shop: Downstream Timeout Surface Negative
# =============================================================================
# Root Cause: 없음. 짧은 burst 부하로 APM latency/error surface만 만들고 upstream
#             external/DPM/KCM root proof는 만들지 않는다.
#
# Expected RCA: preserve uncertainty. Do not conclude external timeout or DB lock.
# =============================================================================

set -uo pipefail

# k3d 도메인별 클러스터: plopvape 전용 kubeconfig (공유 config 의 current-context 드리프트 무관)
export KUBECONFIG=/home/nkia/.kube/plopvape.yaml
NAMESPACE="${NAMESPACE:-rca-testbed-plopvape}"
# k3d 는 호스트로 NodePort 를 publish 하지 않으므로, API 호출은 클러스터 내부에서
# 앱 파드(curl 보유) 를 kubectl exec 경유로 nginx 게이트웨이로 보낸다.
API_BASE="${API_BASE:-http://testbed-nginx-external}"
APP_POD_LABEL="app=testbed-order"
RESULT_LOG="/tmp/g2-plopvape-downstream-timeout-negative.log"
ORDER_ROUNDS=5
CONCURRENT_ORDERS=40

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
log_info()  { echo -e "${CYAN}[INFO]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}   $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }

# --- 클러스터 내부 HTTP 호출 헬퍼 (k3d: 호스트 NodePort 미노출 → 앱 파드 exec 경유) ---
API_POD=""
resolve_api_pod() { API_POD=$(kubectl -n "$NAMESPACE" get pod -l "$APP_POD_LABEL" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null); }
api_curl() { kubectl -n "$NAMESPACE" exec "$API_POD" -- curl "$@"; }

check_prerequisites() {
    resolve_api_pod
    if [[ -z "$API_POD" ]]; then
        log_error "order-service Pod 없음 (API 호출 불가)"
        exit 1
    fi
    local code
    code=$(api_curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$API_BASE/api/products" 2>/dev/null || echo "000")
    if [[ "$code" != "200" ]]; then
        log_error "API not healthy enough for negative scenario (HTTP $code)"
        exit 1
    fi
}

send_order_burst() {
    local round=$1
    # 파드 내부에서 $CONCURRENT_ORDERS 개의 주문을 단일 exec 로 동시 fan-out.
    # (수십~수백 개의 개별 kubectl exec 는 apiserver inflight 한계를 치므로
    #  부하는 앱 파드 안에서 한 번에 생성한다)
    kubectl -n "$NAMESPACE" exec "$API_POD" -- bash -c '
        API_BASE="'"$API_BASE"'"; round="'"$round"'"; concurrent="'"$CONCURRENT_ORDERS"'"
        for i in $(seq 1 "$concurrent"); do
            curl -s -o /dev/null \
                -w "g2-order-${round}-${i}: HTTP %{http_code} in %{time_total}s\n" \
                --max-time 20 \
                -X POST "${API_BASE}/api/orders" \
                -H "Content-Type: application/json" \
                -d "{\"customerName\":\"g2-surface-${round}-${i}\",\"customerEmail\":\"g2-${round}-${i}@test.com\",\"items\":[{\"productId\":$(( i % 16 + 1 )),\"quantity\":1}]}" &
        done
        wait
    ' >> "$RESULT_LOG" 2>&1
}

generate_downstream_surface() {
    log_info "INJECTION_START_MS=$(date +%s%3N)"
    log_info "Generating APM surface with burst orders and no infra/database/external mutation"
    for round in $(seq 1 "$ORDER_ROUNDS"); do
        send_order_burst "$round"
        sleep 5
    done
}

cleanup() {
    rm -f "$RESULT_LOG"
    log_info "INJECTION_END_MS=$(date +%s%3N)"
    log_ok "Cleanup complete"
}

main() {
    if [[ "${1:-}" == "cleanup" ]]; then cleanup; trap - EXIT; exit 0; fi
    echo "============================================================"
    echo "  G-2 plopvape: Downstream Timeout Surface Negative"
    echo "  Start: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "============================================================"
    trap cleanup EXIT
    check_prerequisites
    generate_downstream_surface
    [[ -f "$RESULT_LOG" ]] && tail -n 20 "$RESULT_LOG"
    log_info "No service, DB, external dependency, or KCM state was intentionally changed."
    sleep 30
}

main "$@"
