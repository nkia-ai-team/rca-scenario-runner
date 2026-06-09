#!/usr/bin/env bash
# =============================================================================
# G-8 / plopvape-shop: Deployment Change After Symptom Negative
# =============================================================================
# Root Cause: 없음. 먼저 weak symptom을 만들고 이후 notification rollout을 발생시켜
#             after-the-fact change를 root로 오인하지 않는지 검증한다.
# =============================================================================

set -uo pipefail

export KUBECONFIG=/home/nkia/.kube/plopvape.yaml
NAMESPACE="${NAMESPACE:-rca-testbed-plopvape}"
TARGET_DEPLOY="${TARGET_DEPLOY:-testbed-notification}"
SYMPTOM_DEPLOY="${SYMPTOM_DEPLOY:-testbed-payment}"
# k3d 는 호스트로 NodePort 를 publish 하지 않으므로, API 호출은 클러스터 내부에서
# 앱 파드(curl 보유) 를 kubectl exec 경유로 nginx 게이트웨이로 보낸다.
API_BASE="${API_BASE:-http://testbed-nginx-external}"
APP_POD_LABEL="app=testbed-order"
RESULT_LOG="/tmp/g8-plopvape-deploy-change-after.log"
ORIG_REPLICAS_FILE="/tmp/g8-plopvape-payment-orig-replicas"
CONCURRENT_ORDERS=30
ORDER_ROUNDS=4

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
    if ! kubectl -n "$NAMESPACE" get deploy "$SYMPTOM_DEPLOY" &>/dev/null; then
        log_error "symptom deployment not found: $SYMPTOM_DEPLOY"
        exit 1
    fi
    kubectl -n "$NAMESPACE" get deploy "$SYMPTOM_DEPLOY" -o jsonpath='{.spec.replicas}' > "$ORIG_REPLICAS_FILE"

    resolve_api_pod
    if [[ -z "$API_POD" ]]; then
        log_error "order-service Pod 없음 (API 호출 불가)"
        exit 1
    fi
}

send_order_burst() {
    local round=$1

    # 파드 내부에서 $CONCURRENT_ORDERS 개의 주문을 단일 exec 로 동시 fan-out.
    kubectl -n "$NAMESPACE" exec "$API_POD" -- bash -c '
        API_BASE="'"$API_BASE"'"; round="'"$round"'"; concurrent="'"$CONCURRENT_ORDERS"'"
        for i in $(seq 1 "$concurrent"); do
            curl -s -o /dev/null \
                -w "g8-order-${round}-${i}: HTTP %{http_code} in %{time_total}s\n" \
                --max-time 20 \
                -X POST "${API_BASE}/api/orders" \
                -H "Content-Type: application/json" \
                -d "{\"customerName\":\"g8-symptom-${round}-${i}\",\"customerEmail\":\"g8-${round}-${i}@test.com\",\"items\":[{\"productId\":$(( i % 16 + 1 )),\"quantity\":1}]}" &
        done
        wait
    ' >> "$RESULT_LOG" 2>&1
}

generate_symptom_first() {
    log_info "INJECTION_START_MS=$(date +%s%3N)"
    log_info "Generating service symptom before unrelated notification rollout"
    kubectl -n "$NAMESPACE" scale deploy "$SYMPTOM_DEPLOY" --replicas=0
    sleep 15
    for round in $(seq 1 "$ORDER_ROUNDS"); do
        send_order_burst "$round"
        sleep 5
    done
}

rollout_after_symptom() {
    log_info "CHANGE_AFTER_SYMPTOM_MS=$(date +%s%3N)"
    kubectl -n "$NAMESPACE" rollout restart deploy "$TARGET_DEPLOY"
    kubectl -n "$NAMESPACE" rollout status deploy "$TARGET_DEPLOY" --timeout=180s || true
}

cleanup() {
    local orig=1
    if [[ -f "$ORIG_REPLICAS_FILE" ]]; then
        orig=$(cat "$ORIG_REPLICAS_FILE")
    fi
    kubectl -n "$NAMESPACE" scale deploy "$SYMPTOM_DEPLOY" --replicas="$orig" 2>/dev/null || true
    kubectl -n "$NAMESPACE" rollout status deploy "$SYMPTOM_DEPLOY" --timeout=180s 2>/dev/null || true
    kubectl -n "$NAMESPACE" rollout status deploy "$TARGET_DEPLOY" --timeout=180s 2>/dev/null || true
    rm -f "$RESULT_LOG" "$ORIG_REPLICAS_FILE"
    log_info "INJECTION_END_MS=$(date +%s%3N)"
    log_ok "Cleanup complete (symptom replicas=$orig)"
}

main() {
    if [[ "${1:-}" == "cleanup" ]]; then cleanup; trap - EXIT; exit 0; fi
    echo "============================================================"
    echo "  G-8 plopvape: Deployment Change After Symptom Negative"
    echo "  Start: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "============================================================"
    trap cleanup EXIT
    check_prerequisites
    generate_symptom_first
    rollout_after_symptom
    [[ -f "$RESULT_LOG" ]] && tail -n 30 "$RESULT_LOG"
}

main "$@"
