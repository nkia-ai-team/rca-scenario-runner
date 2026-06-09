#!/usr/bin/env bash
# =============================================================================
# G-4 / plopvape-shop: DPM Bootstrap Only Negative
# =============================================================================
# Root Cause: 없음. DB 주변 약한/직접 psql surface만 만들고 service propagation과
#             current lock proof 없이 DPM conclusive를 방지하는 negative guardrail.
# =============================================================================

set -uo pipefail

# k3d 도메인별 클러스터: plopvape 전용 kubeconfig (공유 config 의 current-context 드리프트 무관)
export KUBECONFIG=/home/nkia/.kube/plopvape.yaml
NAMESPACE="${NAMESPACE:-rca-testbed-plopvape}"
PG_POD="${PG_POD:-testbed-postgres-0}"
# k3d 는 호스트로 NodePort 를 publish 하지 않으므로, API 호출은 클러스터 내부에서
# 앱 파드(curl 보유) 를 kubectl exec 경유로 nginx 게이트웨이로 보낸다.
API_BASE="${API_BASE:-http://testbed-nginx-external}"
APP_POD_LABEL="app=testbed-order"
RESULT_LOG="/tmp/g4-plopvape-dpm-bootstrap-only.log"
DB_PROBE_ROUNDS=12

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
log_info()  { echo -e "${CYAN}[INFO]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}   $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }

# --- 클러스터 내부 HTTP 호출 헬퍼 (k3d: 호스트 NodePort 미노출 → 앱 파드 exec 경유) ---
API_POD=""
resolve_api_pod() { API_POD=$(kubectl -n "$NAMESPACE" get pod -l "$APP_POD_LABEL" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null); }
api_curl() { kubectl -n "$NAMESPACE" exec "$API_POD" -- curl "$@"; }

check_prerequisites() {
    if ! kubectl -n "$NAMESPACE" get pod "$PG_POD" &>/dev/null; then
        log_error "PostgreSQL pod not found"
        exit 1
    fi
    resolve_api_pod
    if [[ -z "$API_POD" ]]; then
        log_error "order-service Pod 없음 (API 호출 불가)"
        exit 1
    fi
    api_curl -s -o /dev/null --max-time 5 "$API_BASE/api/products" || true
}

generate_weak_db_surface() {
    log_info "INJECTION_START_MS=$(date +%s%3N)"
    log_info "Generating direct DB probes with no service-path lock or propagation"
    for i in $(seq 1 "$DB_PROBE_ROUNDS"); do
        kubectl -n "$NAMESPACE" exec "$PG_POD" -- \
            psql -U plopvape -d plopvape -t -A \
            -c "SELECT count(*) FROM product_schema.products; SELECT pg_sleep(1);" \
            >> "$RESULT_LOG" 2>&1 || true
        api_curl -s -o /dev/null -w "g4-products-${i}: HTTP %{http_code} in %{time_total}s\n" \
            --max-time 5 "$API_BASE/api/products" >> "$RESULT_LOG" 2>&1 || true
        sleep 4
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
    echo "  G-4 plopvape: DPM Bootstrap Only Negative"
    echo "  Start: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "============================================================"
    trap cleanup EXIT
    check_prerequisites
    generate_weak_db_surface
    [[ -f "$RESULT_LOG" ]] && tail -n 30 "$RESULT_LOG"
    log_info "No application lock, sub-agent proof, or external dependency injection was created."
    sleep 30
}

main "$@"
