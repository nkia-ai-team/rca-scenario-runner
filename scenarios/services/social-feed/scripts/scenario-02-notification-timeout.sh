#!/usr/bin/env bash
# =============================================================================
# 시나리오 02: Mock Push Gateway Timeout
# =============================================================================
# Root Cause: mock-push-gateway 를 TCP black-hole 로 만들어 notification-service
#             가 외부 push API 호출 시 read-timeout (10s) 발생.
#
# 전파 경로: notification read-timeout → notification thread starvation → 502 응답
#
# 사용법:
#   ./scenario-02-notification-timeout.sh         # 시나리오 실행
#   ./scenario-02-notification-timeout.sh cleanup # 원상복구
# =============================================================================

set -uo pipefail

export KUBECONFIG=/home/nkia/.kube/config
NAMESPACE="rca-testbed-social"
PUSH_DEPLOY="mock-push-gateway"
API_BASE="http://127.0.0.1:30081"
NOTIF_BURST=30
NOTIF_ROUNDS=5
ORIG_REPLICAS_FILE="/tmp/scenario-02-orig-replicas"

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
    if ! kubectl -n "$NAMESPACE" get deploy "$PUSH_DEPLOY" &>/dev/null; then
        log_error "deploy $PUSH_DEPLOY 부재"
        exit 1
    fi
    log_ok "사전 조건 확인 완료"
}

induce_timeout() {
    log_info "mock-push-gateway 를 0 replica 로 축소 (TCP black-hole 효과)"
    kubectl -n "$NAMESPACE" get deploy "$PUSH_DEPLOY" -o jsonpath='{.spec.replicas}' > "$ORIG_REPLICAS_FILE"
    kubectl -n "$NAMESPACE" scale deploy "$PUSH_DEPLOY" --replicas=0
    sleep 5
    log_ok "mock-push-gateway 중단됨"
}

burst_notifications() {
    log_info "알림 발행 폭주: ${NOTIF_BURST} concurrent x ${NOTIF_ROUNDS} rounds"
    for round in $(seq 1 "$NOTIF_ROUNDS"); do
        log_info "Round $round/$NOTIF_ROUNDS"
        for i in $(seq 1 "$NOTIF_BURST"); do
            curl -s -o /dev/null -w "%{http_code} %{time_total}\n" --max-time 15 -X POST \
                "$API_BASE/api/notifications" \
                -H 'Content-Type: application/json' \
                -d "{\"userId\":1,\"message\":\"timeout-test-${round}-${i}\"}" &
        done
        wait
        sleep 10
    done
}

cleanup() {
    log_warn "cleanup: mock-push-gateway 복원"
    local orig=1
    [[ -f "$ORIG_REPLICAS_FILE" ]] && orig=$(cat "$ORIG_REPLICAS_FILE")
    kubectl -n "$NAMESPACE" scale deploy "$PUSH_DEPLOY" --replicas="$orig" 2>/dev/null || true
    rm -f "$ORIG_REPLICAS_FILE"
    log_ok "cleanup 완료 (replicas=$orig 복원)"
}

trap cleanup EXIT

main() {
    if [[ "${1:-}" == "cleanup" ]]; then
        cleanup
        trap - EXIT
        exit 0
    fi
    check_prerequisites
    induce_timeout
    burst_notifications
}

main "$@"
