#!/usr/bin/env bash
# =============================================================================
# 시나리오 01: Feed Entries Row Lock Contention
# =============================================================================
# Root Cause: feed_entries 테이블에 장시간 SELECT FOR UPDATE lock을 점유한
#             상태에서 동시 게시물 작성을 발생시켜 row lock contention 재현.
#
# 전파 경로: feed_entries lock wait → post fan-out timeout → POST /api/posts 5xx → Nginx 502
#
# 사용법:
#   ./scenario-01-feed-entries-lock.sh         # 시나리오 실행
#   ./scenario-01-feed-entries-lock.sh cleanup # 원상복구
#
# 실행 환경: K3s + social-feed 가 배포된 호스트
# =============================================================================

set -uo pipefail

# k3d 도메인별 클러스터: social 전용 kubeconfig (공유 config 의 current-context 드리프트 무관)
export KUBECONFIG=/home/nkia/.kube/social.yaml
NAMESPACE="rca-testbed-social"
DB_POD="testbed-mysql-0"
DB_USER="socialfeed"
DB_PASS="socialfeed1234"
DB_NAME="socialfeed"
# k3d 는 호스트로 NodePort 를 publish 하지 않으므로, API 호출은 클러스터 내부에서
# 앱 파드(curl 보유) 를 kubectl exec 경유로 nginx 게이트웨이로 보낸다.
API_BASE="http://testbed-nginx-external"
APP_POD_LABEL="app=testbed-post"
LOCK_USER_ID=1
LOCK_DURATION=120
CONCURRENT_POSTS=20
POST_ROUNDS=3
LOCK_PID_FILE="/tmp/scenario-01-feed-lock.pid"

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
    if ! kubectl -n "$NAMESPACE" get pod "$DB_POD" &>/dev/null; then
        log_error "MySQL Pod ($DB_POD) 접근 불가"
        exit 1
    fi
    resolve_api_pod
    if [[ -z "$API_POD" ]]; then
        log_error "post-service Pod 없음 (API 호출 불가)"
        exit 1
    fi
    local http_code
    http_code=$(api_curl -s -o /dev/null -w "%{http_code}" "$API_BASE/api/feed/$LOCK_USER_ID" 2>/dev/null || echo "000")
    if [[ "$http_code" != "200" && "$http_code" != "204" && "$http_code" != "404" ]]; then
        log_error "API 접근 불가 (HTTP $http_code)"
        exit 1
    fi
    log_ok "사전 조건 확인 완료"
}

acquire_lock() {
    log_info "feed_entries 테이블 user_id=$LOCK_USER_ID 행에 SELECT FOR UPDATE lock 획득 (${LOCK_DURATION}s 유지)"
    kubectl -n "$NAMESPACE" exec "$DB_POD" -- bash -c "
        mysql -u${DB_USER} -p${DB_PASS} ${DB_NAME} -e '
            START TRANSACTION;
            SELECT * FROM feed_entries WHERE user_id = ${LOCK_USER_ID} FOR UPDATE;
            SELECT SLEEP(${LOCK_DURATION});
            COMMIT;
        ' 2>/dev/null
    " &
    echo $! > "$LOCK_PID_FILE"
    sleep 2
    log_ok "Lock 획득 완료 (PID: $(cat $LOCK_PID_FILE))"
}

burst_posts() {
    log_info "동시 게시물 폭주: ${CONCURRENT_POSTS} concurrent x ${POST_ROUNDS} rounds"
    for round in $(seq 1 "$POST_ROUNDS"); do
        log_info "Round $round/$POST_ROUNDS"
        for i in $(seq 1 "$CONCURRENT_POSTS"); do
            api_curl -s -o /dev/null -w "%{http_code} %{time_total}\n" -X POST \
                "$API_BASE/api/posts" \
                -H 'Content-Type: application/json' \
                -d "{\"authorId\":${LOCK_USER_ID},\"content\":\"viral round-${round}-${i}\"}" &
        done
        wait
        sleep 5
    done
}

cleanup() {
    log_warn "cleanup: lock 해제 + 잔존 트랜잭션 정리"
    if [[ -f "$LOCK_PID_FILE" ]]; then
        local pid
        pid=$(cat "$LOCK_PID_FILE")
        kill "$pid" 2>/dev/null || true
        rm -f "$LOCK_PID_FILE"
    fi
    kubectl -n "$NAMESPACE" exec "$DB_POD" -- bash -c "
        mysql -u${DB_USER} -p${DB_PASS} ${DB_NAME} -e '
            SELECT id, user, time, state FROM information_schema.processlist WHERE command != \"Sleep\" LIMIT 10;
        ' 2>/dev/null || true
    "
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
    acquire_lock
    burst_posts
}

main "$@"
