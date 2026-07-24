#!/usr/bin/env bash
# =============================================================================
# DB-2 / plopvape: PostgreSQL Connection Pool Exhaustion
# =============================================================================
# Root Cause: psql long-running connection(SELECT pg_sleep)으로 postgres
#             max_connections(100)를 소진한다. order-service가 새 DB connection을
#             못 얻어 전 plopvape 서비스가 5xx로 공통 실패한다. lock이 아니라
#             connection 자원 고갈이 원인이다.
#
# 전파 경로: connection 점유 → postgres max_connections 도달 → 앱 새 connection 거부
#            → order/product/inventory 등 전 서비스 5xx
#
# 관측 증거: dpm session count(active_count)↑ + apm 전 서비스 error rate
#            (lock_time은 정상 — non-lock DB 원인 검증)
#
#   ./scenario-14-db-connection-exhaustion.sh           # 실행
#   ./scenario-14-db-connection-exhaustion.sh cleanup   # 원상복구
# =============================================================================

set -uo pipefail

# runner가 default kubeconfig를 주입하므로 강제 지정(plopvape 클러스터, default context 회피).
export KUBECONFIG="${KUBECONFIG:-/root/tb-kubeconfig}"
NAMESPACE="${NAMESPACE:-rca-testbed-plopvape}"
PG_POD="${PG_POD:-testbed-postgres-0}"
# k3d 는 호스트로 NodePort 를 publish 하지 않으므로, API 호출은 클러스터 내부에서
# 앱 파드(curl 보유) 를 kubectl exec 경유로 nginx 게이트웨이로 보낸다.
API_BASE="${API_BASE:-http://testbed-nginx-external}"
APP_POD_LABEL="app=testbed-order"
# max_connections(100) 근접까지 점유. 현재 앱 idle pool(~50)+점유로 고갈 유도.
HOLD_CONNECTIONS="${HOLD_CONNECTIONS:-42}"
LOAD_DURATION_SEC="${LOAD_DURATION_SEC:-360}"
CONCURRENT_ORDERS="${CONCURRENT_ORDERS:-40}"
ROUND_SLEEP_SEC="${ROUND_SLEEP_SEC:-8}"
PID_FILE="/tmp/db2-plopvape-conn-exhaust.pid"
RESULT_LOG="/tmp/db2-plopvape-conn-exhaust.log"
HOLD_TAG="db2_conn_hold"

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

load_creds() {
    PGUSER=$(kubectl -n "$NAMESPACE" get secret postgres-secret -o jsonpath='{.data.POSTGRES_USER}' 2>/dev/null | base64 -d)
    PGPASS=$(kubectl -n "$NAMESPACE" get secret postgres-secret -o jsonpath='{.data.POSTGRES_PASSWORD}' 2>/dev/null | base64 -d)
    PGDB=$(kubectl -n "$NAMESPACE" get secret postgres-secret -o jsonpath='{.data.POSTGRES_DB}' 2>/dev/null | base64 -d)
    if [[ -z "${PGUSER:-}" || -z "${PGPASS:-}" || -z "${PGDB:-}" ]]; then
        log_error "postgres-secret 자격증명 로드 실패"
        exit 1
    fi
}

check_prerequisites() {
    if ! kubectl -n "$NAMESPACE" get pod "$PG_POD" &>/dev/null; then
        log_error "postgres pod not found: $PG_POD"
        exit 1
    fi
    load_creds
    log_info "postgres user=$PGUSER db=$PGDB hold=$HOLD_CONNECTIONS"

    resolve_api_pod
    if [[ -z "$API_POD" ]]; then
        log_error "order-service Pod 없음 (API 호출 불가)"
        exit 1
    fi
}

saturate_connections() {
    log_info "INJECTION_START_MS=$(date +%s%3N)"
    # pod 안에서 HOLD_CONNECTIONS 개의 long-running connection을 점유.
    # application_name=db2_conn_hold 로 태깅해 cleanup에서 정확히 종료.
    kubectl -n "$NAMESPACE" exec "$PG_POD" -- bash -c "
        for i in \$(seq 1 $HOLD_CONNECTIONS); do
            PGPASSWORD='$PGPASS' PGAPPNAME='$HOLD_TAG' psql -U '$PGUSER' -d '$PGDB' \
                -c \"SELECT pg_sleep($LOAD_DURATION_SEC)\" >/dev/null 2>&1 &
        done
        wait" >> "$RESULT_LOG" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 5
    local held
    held=$(kubectl -n "$NAMESPACE" exec "$PG_POD" -- env PGPASSWORD="$PGPASS" psql -U "$PGUSER" -d "$PGDB" -tA \
        -c "SELECT count(*) FROM pg_stat_activity WHERE application_name='$HOLD_TAG'" 2>/dev/null || echo "?")
    log_info "held connections=$held (target $HOLD_CONNECTIONS)"
}

send_orders() {
    local round=$1

    # 파드 내부에서 $CONCURRENT_ORDERS 개의 주문을 단일 exec 로 동시 fan-out.
    kubectl -n "$NAMESPACE" exec "$API_POD" -- bash -c '
        API_BASE="'"$API_BASE"'"; round="'"$round"'"; concurrent="'"$CONCURRENT_ORDERS"'"
        for i in $(seq 1 "$concurrent"); do
            curl -s -o /dev/null \
                -w "db2-order-${round}-${i}: HTTP %{http_code} in %{time_total}s\n" \
                --max-time 35 \
                -X POST "${API_BASE}/api/orders" \
                -H "Content-Type: application/json" \
                -d "{\"customerName\":\"db2-${round}-${i}\",\"customerEmail\":\"db2-${round}-${i}@test.com\",\"items\":[{\"productId\":$(( i % 16 + 1 )),\"quantity\":1}]}" &
        done
        wait
    ' >> "$RESULT_LOG" 2>&1
}

cleanup() {
    # 점유 connection 종료: application_name 기준 pg_terminate_backend
    if [[ -n "${PGUSER:-}" ]]; then
        kubectl -n "$NAMESPACE" exec "$PG_POD" -- env PGPASSWORD="$PGPASS" psql -U "$PGUSER" -d "$PGDB" \
            -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='$HOLD_TAG'" >/dev/null 2>&1 || true
    fi
    if [[ -f "$PID_FILE" ]]; then
        kill "$(cat "$PID_FILE")" 2>/dev/null || true
        rm -f "$PID_FILE"
    fi
    rm -f "$RESULT_LOG"
    log_info "INJECTION_END_MS=$(date +%s%3N)"
    log_ok "Cleanup complete (held connections terminated)"
}

main() {
    if [[ "${1:-}" == "cleanup" ]]; then
        load_creds 2>/dev/null || true
        cleanup
        trap - EXIT
        exit 0
    fi
    echo "============================================================"
    echo "  DB-2 plopvape: PostgreSQL Connection Pool Exhaustion"
    echo "  Start: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "============================================================"
    rm -f "$RESULT_LOG"
    trap cleanup EXIT
    check_prerequisites
    saturate_connections
    local stress_pid round=1
    stress_pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
    while [[ -n "$stress_pid" ]] && kill -0 "$stress_pid" 2>/dev/null; do
        send_orders "$round"
        log_info "round ${round} sent (connections held)"
        round=$((round + 1))
        sleep "$ROUND_SLEEP_SEC"
    done
    wait "$stress_pid" 2>/dev/null || true
    log_info "load window complete (~${LOAD_DURATION_SEC}s)"
    [[ -f "$RESULT_LOG" ]] && tail -n 30 "$RESULT_LOG"
}

main "$@"
