#!/usr/bin/env bash
# =============================================================================
# G-3 / plopvape-shop: Payment Log Row Lock Measured
# =============================================================================
# Root Cause: payment persistence table에 ACCESS EXCLUSIVE lock을 잡고 주문 부하를
#             보내 measured DPM lock positive case를 만든다.
# =============================================================================

set -uo pipefail

# k3d 도메인별 클러스터: plopvape 전용 kubeconfig (공유 config 의 current-context 드리프트 무관)
export KUBECONFIG="${KUBECONFIG:-/root/tb-kubeconfig}"
NAMESPACE="${NAMESPACE:-rca-testbed-plopvape}"
PG_POD="${PG_POD:-testbed-postgres-0}"
# k3d 는 호스트로 NodePort 를 publish 하지 않으므로, API 호출은 클러스터 내부에서
# 앱 파드(curl 보유) 를 kubectl exec 경유로 nginx 게이트웨이로 보낸다.
API_BASE="${API_BASE:-http://testbed-nginx-external}"
APP_POD_LABEL="app=testbed-order"
LOCK_PID_FILE="/tmp/g3-payment-lock.pid"
RESULT_LOG="/tmp/g3-plopvape-payment-lock-results.log"
LOCK_DURATION=120
CONCURRENT_ORDERS=15
ORDER_ROUNDS=3

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log_info()  { echo -e "${CYAN}[INFO]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}   $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }

# --- 클러스터 내부 HTTP 호출 헬퍼 (k3d: 호스트 NodePort 미노출 → 앱 파드 exec 경유) ---
API_POD=""
resolve_api_pod() { API_POD=$(kubectl -n "$NAMESPACE" get pod -l "$APP_POD_LABEL" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null); }
api_curl() { kubectl -n "$NAMESPACE" exec "$API_POD" -- curl "$@"; }

psql_exec() {
    kubectl -n "$NAMESPACE" exec "$PG_POD" -- psql -U plopvape -d plopvape "$@"
}

check_prerequisites() {
    if ! kubectl -n "$NAMESPACE" get pod "$PG_POD" &>/dev/null; then
        log_error "PostgreSQL pod not found: $PG_POD"
        exit 1
    fi
    local table
    table=$(psql_exec -t -A -c "SELECT to_regclass('payment_schema.payments');" 2>/dev/null || true)
    if [[ "$table" != "payment_schema.payments" ]]; then
        log_error "payment_schema.payments table not found"
        exit 1
    fi
    psql_exec -c "UPDATE inventory_schema.inventory SET stock = GREATEST(stock, 200);" 2>/dev/null || true
    resolve_api_pod
    if [[ -z "$API_POD" ]]; then
        log_error "order-service Pod 없음 (API 호출 불가)"
        exit 1
    fi
}

start_payment_lock() {
    log_info "INJECTION_START_MS=$(date +%s%3N)"
    log_info "Starting ACCESS EXCLUSIVE lock on payment_schema.payments for ${LOCK_DURATION}s"
    psql_exec -c "BEGIN; LOCK TABLE payment_schema.payments IN ACCESS EXCLUSIVE MODE; SELECT pg_sleep($LOCK_DURATION); ROLLBACK;" \
        &>/tmp/g3-payment-lock-session.log &
    local pid=$!
    echo "$pid" > "$LOCK_PID_FILE"
    sleep 3
    psql_exec -c "SELECT locktype, relation::regclass, mode, granted, pid FROM pg_locks WHERE relation='payment_schema.payments'::regclass;" \
        2>/dev/null || true
    log_ok "Lock session started (pid=$pid)"
}

send_orders() {
    local round=$1
    log_info "Round $round: sending $CONCURRENT_ORDERS payment-path orders"
    local pids=()
    for i in $(seq 1 "$CONCURRENT_ORDERS"); do
        api_curl -s -o /dev/null \
            -w "g3-order-${round}-${i}: HTTP %{http_code} in %{time_total}s\n" \
            --max-time 45 \
            -X POST "$API_BASE/api/orders" \
            -H "Content-Type: application/json" \
            -d "{\"customerName\":\"g3-lock-${round}-${i}\",\"customerEmail\":\"g3-${round}-${i}@test.com\",\"items\":[{\"productId\":$((i % 16 + 1)),\"quantity\":1}]}" \
            >> "$RESULT_LOG" 2>&1 &
        pids+=($!)
    done
    for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
}

cleanup() {
    if [[ -f "$LOCK_PID_FILE" ]]; then
        local pid
        pid=$(cat "$LOCK_PID_FILE")
        kill "$pid" 2>/dev/null || true
        rm -f "$LOCK_PID_FILE"
    fi
    psql_exec -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='plopvape' AND query LIKE '%payment_schema.payments%' AND pid <> pg_backend_pid();" \
        2>/dev/null || true
    psql_exec -c "UPDATE inventory_schema.inventory SET stock = GREATEST(stock, 50);" 2>/dev/null || true
    rm -f "$RESULT_LOG" /tmp/g3-payment-lock-session.log
    log_info "INJECTION_END_MS=$(date +%s%3N)"
    log_ok "Cleanup complete"
}

main() {
    if [[ "${1:-}" == "cleanup" ]]; then cleanup; trap - EXIT; exit 0; fi
    echo "============================================================"
    echo "  G-3 plopvape: Payment Log Row Lock Measured"
    echo "  Start: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "============================================================"
    rm -f "$RESULT_LOG"
    trap cleanup EXIT
    check_prerequisites
    start_payment_lock
    for round in $(seq 1 "$ORDER_ROUNDS"); do
        send_orders "$round"
        sleep 5
    done
    [[ -f "$RESULT_LOG" ]] && tail -n 30 "$RESULT_LOG"
}

main "$@"
