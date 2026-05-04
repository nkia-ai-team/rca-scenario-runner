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

export KUBECONFIG=/home/nkia/.kube/config
NAMESPACE="rca-testbed-social"
API_BASE="http://127.0.0.1:30081"
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

check_prerequisites() {
    log_info "사전 조건 확인 중..."
    local http_code
    http_code=$(curl -s -o /dev/null -w "%{http_code}" "$API_BASE/api/feed/1" --max-time 5 2>/dev/null || echo "000")
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
        for i in $(seq 1 "$concurrent"); do
            curl -s -o /dev/null -w "%{http_code} %{time_total}\n" --max-time 30 -X POST \
                "$API_BASE/api/posts" \
                -H 'Content-Type: application/json' \
                -d "{\"userId\":$((i % 5 + 1)),\"content\":\"flood-${concurrent}-${i}-$(date +%s)\"}" &
        done
        wait
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
