#!/usr/bin/env bash
# =============================================================================
# DS-1 / plopvape: 데이터 상태 오류(전 상품 품절) → 4xx 폭주
# =============================================================================
# Root Cause: 재고 데이터가 전부 0(품절) 상태가 되어 주문이 비즈니스 규칙상 거부된다.
#             인프라/5xx 장애가 아니라 데이터/비즈니스 상태가 원인 → 4xx(409) 폭주.
#             "5xx 인프라 장애"와 구분되는 진단 변별 시나리오.
#
# 전파 경로: 재고 0(데이터 상태) → 주문 검증 실패 → 409 Conflict 폭주(4xx, 5xx 아님)
#
# 관측 증거(2026-06-05 실측): apm httpStsCd 4xx(409). 5xx 아님이 핵심 변별.
#   ⚠️ RCA 의 apm 채널은 4xx 를 trace fallback(coverage_ok=False, degraded)로만
#   surface — 신호는 104 에 있으나 RCA 읽기는 degraded(T1-degraded/T2, CH 무관).
#
#   ./scenario-19-data-state-4xx-flood.sh           # 실행
#   ./scenario-19-data-state-4xx-flood.sh cleanup   # 원상복구(재고 복원)
# =============================================================================
set -uo pipefail
export KUBECONFIG="${KUBECONFIG:-/root/tb-kubeconfig}"
NAMESPACE="${NAMESPACE:-rca-testbed-plopvape}"
PG_POD="${PG_POD:-testbed-postgres-0}"
# k3d: 호스트 NodePort/노드 IP(172.x) 미노출·취약 → 클러스터 내부 nginx 를 앱 파드 exec 로 호출.
API_BASE="${API_BASE:-http://testbed-nginx-external}"
APP_POD_LABEL="${APP_POD_LABEL:-app=testbed-order}"
RESTORE_STOCK="${RESTORE_STOCK:-1000}"   # cleanup 시 복원 재고
LOAD_DURATION_SEC="${LOAD_DURATION_SEC:-300}"
CONCURRENT_ORDERS="${CONCURRENT_ORDERS:-15}"
ROUND_SLEEP_SEC="${ROUND_SLEEP_SEC:-4}"
RESULT_LOG="/tmp/ds1-plopvape-4xx.log"
CYAN='\033[0;36m'; GREEN='\033[0;32m'; NC='\033[0m'
log_info(){ echo -e "${CYAN}[INFO]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_ok(){ echo -e "${GREEN}[OK]${NC}   $(date '+%Y-%m-%d %H:%M:%S') $*"; }
# 클러스터 내부 HTTP 호출 헬퍼 (k3d: 호스트 NodePort 미노출 → 앱 파드 exec 경유)
API_POD=""
resolve_api_pod(){ API_POD=$(kubectl -n "$NAMESPACE" get pod -l "$APP_POD_LABEL" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null); }
api_curl(){ kubectl -n "$NAMESPACE" exec "$API_POD" -- curl "$@"; }
load_creds(){
    PGUSER=$(kubectl -n "$NAMESPACE" get secret postgres-secret -o jsonpath='{.data.POSTGRES_USER}' 2>/dev/null|base64 -d)
    PGPASS=$(kubectl -n "$NAMESPACE" get secret postgres-secret -o jsonpath='{.data.POSTGRES_PASSWORD}' 2>/dev/null|base64 -d)
    PGDB=$(kubectl -n "$NAMESPACE" get secret postgres-secret -o jsonpath='{.data.POSTGRES_DB}' 2>/dev/null|base64 -d)
}
psql_q(){ kubectl -n "$NAMESPACE" exec "$PG_POD" -- env PGPASSWORD="$PGPASS" psql -U "$PGUSER" -d "$PGDB" -tA -c "$1" 2>/dev/null; }
inject(){
    echo "INJECTION_START_MS=$(date +%s%3N)"
    log_info "=== 데이터 상태 오류 주입: 전 상품 재고 0(품절) ==="
    psql_q "UPDATE inventory_schema.inventory SET stock=0" >/dev/null
    log_info "재고 0 처리 rows=$(psql_q 'SELECT count(*) FROM inventory_schema.inventory WHERE stock=0')"
}
send_orders(){
    local round=$1 pids=()
    for i in $(seq 1 "$CONCURRENT_ORDERS"); do
        api_curl -s -o /dev/null -w "ds1-order-${round}-${i}: HTTP %{http_code}\n" --max-time 20 \
            -X POST "$API_BASE/api/orders" -H "Content-Type: application/json" \
            -d "{\"customerName\":\"ds1-${round}-${i}\",\"customerEmail\":\"ds1-${round}-${i}@test.com\",\"items\":[{\"productId\":$((i%16+1)),\"quantity\":1}]}" \
            >> "$RESULT_LOG" 2>&1 &
        pids+=($!)
    done
    for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
}
cleanup(){
    log_info "=== cleanup: 재고 복원($RESTORE_STOCK) ==="
    load_creds 2>/dev/null || true
    [[ -n "${PGUSER:-}" ]] && psql_q "UPDATE inventory_schema.inventory SET stock=$RESTORE_STOCK" >/dev/null 2>&1 || true
    rm -f "$RESULT_LOG"
    echo "INJECTION_END_MS=$(date +%s%3N)"
    log_ok "Cleanup complete"
}
main(){
    if [[ "${1:-}" == "cleanup" ]]; then cleanup; trap - EXIT; exit 0; fi
    echo "=== DS-1 plopvape: Data-State 4xx Flood ($(date '+%F %T')) ==="
    rm -f "$RESULT_LOG"; trap cleanup EXIT
    load_creds
    resolve_api_pod
    [[ -z "$API_POD" ]] && { echo "order-service Pod 없음 — API 호출 불가" >&2; exit 1; }
    inject
    local end_time=$(( $(date +%s) + LOAD_DURATION_SEC )) round=1
    while [[ $(date +%s) -lt $end_time ]]; do
        send_orders "$round"; log_info "round ${round} 전송(품절 4xx 유지)"; round=$((round+1)); sleep "$ROUND_SLEEP_SEC"
    done
    [[ -f "$RESULT_LOG" ]] && { echo "--- HTTP code 분포 ---"; grep -oE 'HTTP [0-9]+' "$RESULT_LOG"|sort|uniq -c; }
}
main "$@"
