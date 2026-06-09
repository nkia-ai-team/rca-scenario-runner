#!/usr/bin/env bash
# =============================================================================
# 시나리오 04: Viral Post Traffic Flood
# =============================================================================
# Root Cause: 4단계 점진적 동시 게시물 트래픽 폭주로 post-service thread pool
#             포화 + DB 커넥션/락 경합 유발.
#
# 전파 경로: post thread pool 포화 → fan-out 하위 서비스 cascading 5xx
#             → DB 세션/Lock 급증
#
# 사용법:
#   ./scenario-04-traffic-flood.sh         # 시나리오 실행
#   ./scenario-04-traffic-flood.sh cleanup # 원상복구
# =============================================================================

set -uo pipefail

# k3d 도메인별 클러스터: social 전용 kubeconfig (공유 config 의 current-context 드리프트 무관)
export KUBECONFIG=/home/nkia/.kube/social.yaml
NAMESPACE="rca-testbed-social"
# k3d 는 호스트로 NodePort 를 publish 하지 않으므로, API 호출은 클러스터 내부에서
# 앱 파드(curl 보유) 를 kubectl exec 경유로 nginx 게이트웨이로 보낸다.
API_BASE="http://testbed-nginx-external"
APP_POD_LABEL="app=testbed-post"
STAGES=(5 50 200 500)
ROUND_DURATION=30
LOG_DIR="/tmp/scenario-04-traffic-flood-logs"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info()  { echo -e "${CYAN}[INFO]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}   $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }
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
    log_info "사전 조건 확인 중..."
    resolve_api_pod
    if [[ -z "$API_POD" ]]; then
        log_error "post-service Pod 없음 (API 호출 불가)"
        exit 1
    fi
    local http_code
    http_code=$(api_curl -s -o /dev/null -w "%{http_code}" "$API_BASE/api/feed/1" --max-time 5 2>/dev/null || echo "000")
    if [[ "$http_code" == "000" ]]; then
        log_error "API 도달 불가 ($API_BASE)"
        exit 1
    fi
    mkdir -p "$LOG_DIR"
    log_ok "사전 조건 확인 완료"
}

stage_burst() {
    local concurrent="$1"
    log_info "Stage: ${concurrent} concurrent posts (${ROUND_DURATION}s)"
    local end=$(( $(date +%s) + ROUND_DURATION ))
    while [[ $(date +%s) -lt $end ]]; do
        # 파드 내부에서 $concurrent 개를 단일 exec 로 동시 fan-out
        # (k3d: 호스트→ClusterIP 직접 불가 + 수백 개 개별 exec 는 apiserver inflight 한계)
        kubectl -n "$NAMESPACE" exec "$API_POD" -- bash -c '
            API_BASE="'"$API_BASE"'"; concurrent="'"$concurrent"'"
            for i in $(seq 1 "$concurrent"); do
                curl -s -o /dev/null -w "%{http_code} %{time_total}\n" --max-time 30 -X POST \
                    "${API_BASE}/api/posts" \
                    -H "Content-Type: application/json" \
                    -d "{\"authorId\":$(( i % 5 + 1 )),\"content\":\"flood-${concurrent}-${i}-$(date +%s)\"}" &
            done
            wait
        '
    done >> "${LOG_DIR}/stage-${concurrent}.log"
}

run_flood() {
    for c in "${STAGES[@]}"; do
        stage_burst "$c"
        sleep 5
    done
}

cleanup() {
    log_warn "cleanup: 잔존 background curl 정리 + 임시 로그 보존"
    pkill -P $$ 2>/dev/null || true
    log_info "stage logs in $LOG_DIR (분석 후 수동 삭제 권고)"
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
    run_flood
}

main "$@"
