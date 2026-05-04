#!/usr/bin/env bash
# =============================================================================
# 시나리오 03: MySQL Pod CPU Throttle
# =============================================================================
# Root Cause: mysql pod CPU limit 을 10m 로 축소하여 극심한 CPU throttling 유발.
#             모든 서비스 쿼리 응답 지연 → 전 서비스 5xx 캐스케이드.
#
# 전파 경로: DB slow query → 모든 endpoint 평균 3~5s, 서비스 에러율 급증
#
# 사용법:
#   ./scenario-03-mysql-cpu-throttle.sh         # 시나리오 실행
#   ./scenario-03-mysql-cpu-throttle.sh cleanup # 원상복구
# =============================================================================

set -uo pipefail

export KUBECONFIG=/home/nkia/.kube/config
NAMESPACE="rca-testbed-social"
DB_STS="testbed-mysql"
API_BASE="http://127.0.0.1:30081"
THROTTLE_DURATION=180
QUERY_BURST=15
QUERY_ROUNDS=8
ORIG_LIMITS_FILE="/tmp/scenario-03-mysql-orig-limits.json"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info()  { echo -e "${CYAN}[INFO]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}   $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }

check_prerequisites() {
    log_info "사전 조건 확인 중..."
    if ! kubectl -n "$NAMESPACE" get sts "$DB_STS" &>/dev/null; then
        log_error "StatefulSet $DB_STS 부재"
        exit 1
    fi
    log_ok "사전 조건 확인 완료"
}

throttle() {
    log_info "현재 mysql container resource limits 백업"
    kubectl -n "$NAMESPACE" get sts "$DB_STS" \
        -o jsonpath='{.spec.template.spec.containers[?(@.name=="mysql")].resources}' > "$ORIG_LIMITS_FILE"
    log_info "mysql CPU limit 을 10m 로 축소 (CPU throttling 유발)"
    kubectl -n "$NAMESPACE" patch sts "$DB_STS" --type='json' -p='[
        {"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/cpu","value":"10m"}
    ]'
    log_info "Pod 재기동 대기 (rollout)"
    kubectl -n "$NAMESPACE" rollout status sts/"$DB_STS" --timeout=120s || true
    sleep 10
}

burst_queries() {
    log_info "전 endpoint 폭주 (DB slow query 효과 검증): ${QUERY_BURST} x ${QUERY_ROUNDS} rounds"
    for round in $(seq 1 "$QUERY_ROUNDS"); do
        log_info "Round $round/$QUERY_ROUNDS"
        for i in $(seq 1 "$QUERY_BURST"); do
            curl -s -o /dev/null -w "feed:%{http_code}/%{time_total}\n"     --max-time 20 "$API_BASE/api/feed/$((i % 5 + 1))" &
            curl -s -o /dev/null -w "comment:%{http_code}/%{time_total}\n"  --max-time 20 "$API_BASE/api/posts/$((i % 5 + 1))/comments" &
        done
        wait
        sleep 5
    done
}

cleanup() {
    log_warn "cleanup: mysql resource limits 복원"
    if [[ -f "$ORIG_LIMITS_FILE" ]]; then
        local orig
        orig=$(cat "$ORIG_LIMITS_FILE")
        if [[ -n "$orig" && "$orig" != "{}" ]]; then
            kubectl -n "$NAMESPACE" patch sts "$DB_STS" --type='json' -p='[
                {"op":"replace","path":"/spec/template/spec/containers/0/resources","value":'"$orig"'}
            ]' 2>/dev/null || true
        fi
        rm -f "$ORIG_LIMITS_FILE"
    fi
    kubectl -n "$NAMESPACE" rollout status sts/"$DB_STS" --timeout=120s 2>/dev/null || true
    log_ok "cleanup 완료"
}

trap cleanup EXIT

main() {
    if [[ "${1:-}" == "cleanup" ]]; then
        cleanup
        trap - EXIT
        exit 0
    fi
    check_prerequisites
    throttle
    burst_queries
    log_info "Throttle 유지 ${THROTTLE_DURATION}s 후 cleanup"
    sleep "$THROTTLE_DURATION"
}

main "$@"
