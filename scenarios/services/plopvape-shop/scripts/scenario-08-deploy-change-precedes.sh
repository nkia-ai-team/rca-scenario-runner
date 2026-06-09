#!/usr/bin/env bash
# =============================================================================
# G-7 / plopvape-shop: Deployment Change Precedes Symptom
# =============================================================================
# Root Cause: payment deployment rollout restart를 먼저 발생시키고 곧바로 주문 부하를
#             보내 change-before-symptom positive case를 만든다.
# =============================================================================

set -uo pipefail

export KUBECONFIG=/home/nkia/.kube/plopvape.yaml
NAMESPACE="${NAMESPACE:-rca-testbed-plopvape}"
TARGET_DEPLOY="${TARGET_DEPLOY:-testbed-payment}"
# k3d 는 호스트로 NodePort 를 publish 하지 않으므로, API 호출은 클러스터 내부에서
# 앱 파드(curl 보유) 를 kubectl exec 경유로 nginx 게이트웨이로 보낸다.
API_BASE="${API_BASE:-http://testbed-nginx-external}"
APP_POD_LABEL="app=testbed-order"
ORIG_REPLICAS_FILE="/tmp/g7-plopvape-payment-orig-replicas"
RESULT_LOG="/tmp/g7-plopvape-deploy-change-precedes.log"
CONCURRENT_ORDERS=30
ORDER_ROUNDS=5

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
log_info()  { echo -e "${CYAN}[INFO]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}   $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }

# --- 클러스터 내부 HTTP 호출 헬퍼 (k3d: 호스트 NodePort 미노출 → 앱 파드 exec 경유) ---
API_POD=""
resolve_api_pod() {
    API_POD=$(kubectl -n "$NAMESPACE" get pod -l "$APP_POD_LABEL" \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
}
# api_curl: curl 과 동일 인자. URL 은 $API_BASE(=cluster nginx) 기준. 클러스터 안에서 실행됨.
api_curl() { kubectl -n "$NAMESPACE" exec "$API_POD" -- curl "$@"; }

check_prerequisites() {
    if ! kubectl -n "$NAMESPACE" get deploy "$TARGET_DEPLOY" &>/dev/null; then
        log_error "deployment not found: $TARGET_DEPLOY"
        exit 1
    fi
    kubectl -n "$NAMESPACE" get deploy "$TARGET_DEPLOY" -o jsonpath='{.spec.replicas}' > "$ORIG_REPLICAS_FILE"

    resolve_api_pod
    if [[ -z "$API_POD" ]]; then
        log_error "order-service Pod 없음 (API 호출 불가)"
        exit 1
    fi
}

rollout_change() {
    log_info "INJECTION_START_MS=$(date +%s%3N)"
    kubectl -n "$NAMESPACE" scale deploy "$TARGET_DEPLOY" --replicas=0
    sleep 15
    kubectl -n "$NAMESPACE" get deploy "$TARGET_DEPLOY" -o wide || true
}

send_orders() {
    local round=$1

    # 파드 내부에서 $CONCURRENT_ORDERS 개의 주문을 단일 exec 로 동시 fan-out.
    kubectl -n "$NAMESPACE" exec "$API_POD" -- bash -c '
        API_BASE="'"$API_BASE"'"; round="'"$round"'"; concurrent="'"$CONCURRENT_ORDERS"'"
        for i in $(seq 1 "$concurrent"); do
            curl -s -o /dev/null \
                -w "g7-order-${round}-${i}: HTTP %{http_code} in %{time_total}s\n" \
                --max-time 35 \
                -X POST "${API_BASE}/api/orders" \
                -H "Content-Type: application/json" \
                -d "{\"customerName\":\"g7-change-${round}-${i}\",\"customerEmail\":\"g7-${round}-${i}@test.com\",\"items\":[{\"productId\":$(( i % 16 + 1 )),\"quantity\":1}]}" &
        done
        wait
    ' >> "$RESULT_LOG" 2>&1
}

cleanup() {
    local orig=1
    if [[ -f "$ORIG_REPLICAS_FILE" ]]; then
        orig=$(cat "$ORIG_REPLICAS_FILE")
    fi
    kubectl -n "$NAMESPACE" scale deploy "$TARGET_DEPLOY" --replicas="$orig" 2>/dev/null || true
    kubectl -n "$NAMESPACE" rollout status deploy "$TARGET_DEPLOY" --timeout=180s 2>/dev/null || true
    rm -f "$RESULT_LOG" "$ORIG_REPLICAS_FILE"
    log_info "INJECTION_END_MS=$(date +%s%3N)"
    log_ok "Cleanup complete (replicas=$orig)"
}

main() {
    if [[ "${1:-}" == "cleanup" ]]; then cleanup; trap - EXIT; exit 0; fi
    echo "============================================================"
    echo "  G-7 plopvape: Deployment Change Precedes Symptom"
    echo "  Start: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "============================================================"
    rm -f "$RESULT_LOG"
    trap cleanup EXIT
    check_prerequisites
    rollout_change
    for round in $(seq 1 "$ORDER_ROUNDS"); do
        send_orders "$round"
        sleep 5
    done
    [[ -f "$RESULT_LOG" ]] && tail -n 30 "$RESULT_LOG"
}

main "$@"
