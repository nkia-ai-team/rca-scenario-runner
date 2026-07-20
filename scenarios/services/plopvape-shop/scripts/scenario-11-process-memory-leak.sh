#!/usr/bin/env bash
# =============================================================================
# P-2 / plopvape-shop: Service Process Memory Leak
# =============================================================================
# Root Cause: order-service(java) 프로세스가 캐시를 무한 적재해 heap/RSS가 단조
#             증가한다. GC 빈도↑ → RT↑ → 최종적으로 OOMKilled/Pod restart 까지
#             진행될 수 있다. root는 그 서비스 프로세스의 메모리 누수다.
#
# 전파 경로: order-service 캐시 무한 적재 → java MEM%/Heap↑ → GcTime↑ → order RT↑
#            → OOMKilled → Pod restart
#
# 관측 증거: 프로세스 java MEM%/RSS 단조 증가(server.Process) + APM HeapUsage/GcTime
#            + KCM Pod restart/OOMKilled lifecycle
# 참고: ref-testbed-architecture.md §5.2 "메모리 누수" (order-service 캐시 무한 적재)
#
# NOTE(주입 검증): 실제 단조 누수 재현은 testbed order-service의 캐시 적재 경로에
#   의존한다. 109 주입 시 heap이 실제로 단조 증가하는지(HeapUsage trend)를 fixture
#   생성 전에 확인한다. 캐시 적재 엔드포인트가 없으면 app side 보강이 선행 조건이다.
#
#   ./scenario-11-process-memory-leak.sh           # 시나리오 실행
#   ./scenario-11-process-memory-leak.sh cleanup   # 원상복구
# =============================================================================

set -uo pipefail

# k3d 도메인별 클러스터: plopvape 전용 kubeconfig (공유 config 의 current-context 드리프트 무관)
export KUBECONFIG="${KUBECONFIG:-/root/tb-kubeconfig}"
NAMESPACE="${NAMESPACE:-rca-testbed-plopvape}"
TARGET_DEPLOY="${TARGET_DEPLOY:-testbed-order}"
# k3d 는 호스트로 NodePort 를 publish 하지 않으므로, API 호출은 클러스터 내부에서
# 앱 파드(curl 보유) 를 kubectl exec 경유로 nginx 게이트웨이로 보낸다.
API_BASE="${API_BASE:-http://testbed-nginx-external}"
APP_POD_LABEL="app=testbed-order"
# 고유 데이터 대량 주문으로 캐시 적재를 가속. 라운드↑ = heap 압박↑.
# SMS 5분 입도 + heap 단조 증가 관측을 위해 ~6분 이상 지속 적재한다.
CONCURRENT_ORDERS="${CONCURRENT_ORDERS:-40}"
ORDER_ROUNDS="${ORDER_ROUNDS:-45}"
ROUND_SLEEP_SEC="${ROUND_SLEEP_SEC:-6}"
RESULT_LOG="/tmp/p2-plopvape-process-memory-leak.log"

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

check_prerequisites() {
    if ! kubectl -n "$NAMESPACE" get deploy "$TARGET_DEPLOY" &>/dev/null; then
        log_error "deployment not found: $TARGET_DEPLOY (ns=$NAMESPACE)"
        exit 1
    fi

    resolve_api_pod
    if [[ -z "$API_POD" ]]; then
        log_error "order-service Pod 없음 (API 호출 불가)"
        exit 1
    fi
}

send_orders() {
    local round=$1

    # 파드 내부에서 $CONCURRENT_ORDERS 개의 주문을 단일 exec 로 동시 fan-out.
    # 라운드/인덱스로 고유 customer 데이터 → 캐시 키 다양화로 적재 가속
    kubectl -n "$NAMESPACE" exec "$API_POD" -- bash -c '
        API_BASE="'"$API_BASE"'"; round="'"$round"'"; concurrent="'"$CONCURRENT_ORDERS"'"
        ts=$(date +%s%3N)
        for i in $(seq 1 "$concurrent"); do
            curl -s -o /dev/null \
                -w "p2-order-${round}-${i}: HTTP %{http_code} in %{time_total}s\n" \
                --max-time 35 \
                -X POST "${API_BASE}/api/orders" \
                -H "Content-Type: application/json" \
                -d "{\"customerName\":\"p2-leak-r${round}-i${i}-${ts}\",\"customerEmail\":\"p2-r${round}-i${i}@test.com\",\"items\":[{\"productId\":$(( i % 16 + 1 )),\"quantity\":1}]}" &
        done
        wait
    ' >> "$RESULT_LOG" 2>&1
}

cleanup() {
    # heap 정상화: order-service rollout restart 로 누적 캐시 회수
    kubectl -n "$NAMESPACE" rollout restart deploy "$TARGET_DEPLOY" 2>/dev/null || true
    kubectl -n "$NAMESPACE" rollout status deploy "$TARGET_DEPLOY" --timeout=180s 2>/dev/null || true
    rm -f "$RESULT_LOG"
    log_info "INJECTION_END_MS=$(date +%s%3N)"
    log_ok "Cleanup complete (order-service restarted, heap reclaimed)"
}

main() {
    if [[ "${1:-}" == "cleanup" ]]; then cleanup; trap - EXIT; exit 0; fi
    echo "============================================================"
    echo "  P-2 plopvape: Service Process Memory Leak"
    echo "  Start: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "============================================================"
    rm -f "$RESULT_LOG"
    trap cleanup EXIT
    check_prerequisites
    log_info "INJECTION_START_MS=$(date +%s%3N)"
    for round in $(seq 1 "$ORDER_ROUNDS"); do
        send_orders "$round"
        log_info "round ${round}/${ORDER_ROUNDS} done (heap pressure ramping)"
        sleep "$ROUND_SLEEP_SEC"
    done
    [[ -f "$RESULT_LOG" ]] && tail -n 30 "$RESULT_LOG"
}

main "$@"
