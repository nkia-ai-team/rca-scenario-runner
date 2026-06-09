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

# k3d 도메인별 클러스터: social 전용 kubeconfig (공유 config 의 current-context 드리프트 무관)
export KUBECONFIG=/home/nkia/.kube/social.yaml
NAMESPACE="rca-testbed-social"
PUSH_DEPLOY="testbed-mock-push-gateway"
PUSH_GATEWAY_URL="http://testbed-mock-push-gateway:1080"   # MockServer (제어/모킹 동일 포트)
# k3d 는 호스트로 NodePort 를 publish 하지 않으므로, API 호출은 클러스터 내부에서
# 앱 파드(curl 보유) 를 kubectl exec 경유로 nginx 게이트웨이로 보낸다.
API_BASE="http://testbed-nginx-external"
APP_POD_LABEL="app=testbed-post"
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

# --- 클러스터 내부 HTTP 호출 헬퍼 (k3d: 호스트 NodePort 미노출 → 앱 파드 exec 경유) ---
API_POD=""
resolve_api_pod() {
    API_POD=$(kubectl -n "$NAMESPACE" get pod -l "$APP_POD_LABEL" \
        -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
}
# api_curl: curl 과 동일 인자. URL 은 $API_BASE(=cluster nginx) 기준. 클러스터 안에서 실행됨.
api_curl() { kubectl -n "$NAMESPACE" exec "$API_POD" -- curl "$@"; }

# mock-push-gateway(MockServer) 가 POST /push 에 200 을 주도록 expectation 등록(멱등).
# 미설정 시 MockServer 기본 404 → notification 이 baseline 부터 502/504 → 시나리오 무의미.
# MockServer(JVM) 는 기동에 수십 초 걸리고 readinessProbe 가 없어 ready 가 일찍 뜨므로,
# HTTP 가 실제로 /push 200 을 줄 때까지 폴링하며 등록한다 (scale 0→1 복구 시 신규 파드 대비).
setup_push_stub() {
    local i code
    for i in $(seq 1 30); do
        api_curl -s -o /dev/null --max-time 5 -X PUT \
            "${PUSH_GATEWAY_URL}/mockserver/expectation" \
            -H 'Content-Type: application/json' \
            -d '{"httpRequest":{"method":"POST","path":"/push"},"httpResponse":{"statusCode":200,"body":"sent"},"times":{"unlimited":true}}' \
            2>/dev/null || true
        code=$(api_curl -s -o /dev/null -w "%{http_code}" --max-time 5 -X POST "${PUSH_GATEWAY_URL}/push" 2>/dev/null || echo "000")
        [[ "$code" == "200" ]] && return 0
        sleep 2
    done
    log_warn "push stub 등록 확인 실패 (MockServer 미응답)"
    return 1
}

check_prerequisites() {
    log_info "사전 조건 확인 중..."
    if ! kubectl -n "$NAMESPACE" get deploy "$PUSH_DEPLOY" &>/dev/null; then
        log_error "deploy $PUSH_DEPLOY 부재"
        exit 1
    fi
    resolve_api_pod
    if [[ -z "$API_POD" ]]; then
        log_error "post-service Pod 없음 (API 호출 불가)"
        exit 1
    fi
    # MockServer 가 POST /push 에 200 을 주도록 보장 후 baseline 확인 (200 기대)
    setup_push_stub
    local base_code
    base_code=$(api_curl -s -o /dev/null -w "%{http_code}" --max-time 10 -X POST \
        "$API_BASE/api/notifications" -H 'Content-Type: application/json' \
        -d '{"userId":1,"type":"NEW_POST","refId":0}' 2>/dev/null || echo "000")
    log_info "baseline 알림 발행: HTTP $base_code (200 기대 — push stub 정상)"
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
            api_curl -s -o /dev/null -w "%{http_code} %{time_total}\n" --max-time 15 -X POST \
                "$API_BASE/api/notifications" \
                -H 'Content-Type: application/json' \
                -d "{\"userId\":1,\"type\":\"NEW_POST\",\"refId\":${i}}" &
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
    # 새 MockServer 파드는 expectation 이 비어있으므로(기본 404) ready 후 /push stub 재등록
    kubectl -n "$NAMESPACE" rollout status deploy "$PUSH_DEPLOY" --timeout=60s 2>/dev/null || true
    resolve_api_pod
    setup_push_stub
    log_ok "cleanup 완료 (replicas=$orig 복원 + push stub 재등록)"
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
