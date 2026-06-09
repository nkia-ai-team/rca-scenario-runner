#!/usr/bin/env bash
# =============================================================================
# P-1 / plopvape-shop: Noisy-Neighbor Host CPU Saturation
# =============================================================================
# Root Cause: 서비스와 무관한 프로세스(stress-ng, 대안: VLLM python 추론 폭주)가 109
#             호스트의 CPU 코어를 점유해 같은 호스트의 plopvape 전 서비스가 공통
#             슬로우다운된다. 단일 서비스 코드 버그가 아니라 호스트/프로세스 레벨
#             공통 원인이다.
#
# 전파 경로: noisy 프로세스 CPU 점유 → 호스트 CPU 포화 → java 서비스 스케줄링 지연
#            → plopvape 전 서비스 RT 동시 상승 → Nginx 응답 지연
#
# 관측 증거: SMS 호스트 CPU_usage + 프로세스 CPU%(server.Process) + APM 전 서비스 RT
# 참고: ref-testbed-architecture.md §5.2 "VLLM 리소스 경합", §6 python=Noisy Neighbor
#       PIMS #121302 (stress-ng --cpu 7) 재현
#
#   ./scenario-10-noisy-neighbor-cpu.sh           # 시나리오 실행
#   ./scenario-10-noisy-neighbor-cpu.sh cleanup   # 원상복구
# =============================================================================

set -uo pipefail

# k3d 도메인별 클러스터: plopvape 전용 kubeconfig (공유 config 의 current-context 드리프트 무관)
export KUBECONFIG=/home/nkia/.kube/plopvape.yaml
NAMESPACE="${NAMESPACE:-rca-testbed-plopvape}"
# k3d 는 호스트로 NodePort 를 publish 하지 않으므로, API 호출은 클러스터 내부에서
# 앱 파드(curl 보유) 를 kubectl exec 경유로 nginx 게이트웨이로 보낸다.
API_BASE="${API_BASE:-http://testbed-nginx-external}"
APP_POD_LABEL="app=testbed-order"
# 비-서비스 프로세스가 점유할 CPU worker 수. 기본은 가용 코어의 대부분.
# 컨테이너 cgroup 무제한 가정. nproc-1 worker로 호스트 CPU 포화.
CPU_WORKERS="${CPU_WORKERS:-$(( $(nproc) > 1 ? $(nproc) - 1 : 1 ))}"
# SMS measurement_metric_5min(5분 입도)를 확실히 덮도록 부하를 6분 유지한다.
LOAD_DURATION_SEC="${LOAD_DURATION_SEC:-360}"
CONCURRENT_ORDERS=20
ROUND_SLEEP_SEC="${ROUND_SLEEP_SEC:-8}"
PID_FILE="/tmp/p1-plopvape-noisy-neighbor.pid"
RESULT_LOG="/tmp/p1-plopvape-noisy-neighbor.log"

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
    if ! command -v stress-ng &>/dev/null; then
        log_error "stress-ng not found. 설치: sudo yum install stress-ng (RHEL) / sudo apt install stress-ng (Debian)"
        exit 1
    fi
    log_info "stress-ng available; CPU_WORKERS=$CPU_WORKERS (nproc=$(nproc))"

    resolve_api_pod
    if [[ -z "$API_POD" ]]; then
        log_error "order-service Pod 없음 (API 호출 불가)"
        exit 1
    fi
}

saturate_cpu() {
    log_info "INJECTION_START_MS=$(date +%s%3N)"
    # 비-서비스 프로세스로 호스트 CPU 포화. 백그라운드 실행 후 PID 보존.
    stress-ng --cpu "$CPU_WORKERS" --timeout "${LOAD_DURATION_SEC}s" --metrics-brief \
        >> "$RESULT_LOG" 2>&1 &
    echo $! > "$PID_FILE"
    log_info "noisy-neighbor stress-ng started (pid=$(cat "$PID_FILE"), ${LOAD_DURATION_SEC}s)"
}

send_orders() {
    local round=$1

    # 파드 내부에서 $CONCURRENT_ORDERS 개의 주문을 단일 exec 로 동시 fan-out.
    kubectl -n "$NAMESPACE" exec "$API_POD" -- bash -c '
        API_BASE="'"$API_BASE"'"; round="'"$round"'"; concurrent="'"$CONCURRENT_ORDERS"'"
        for i in $(seq 1 "$concurrent"); do
            curl -s -o /dev/null \
                -w "p1-order-${round}-${i}: HTTP %{http_code} in %{time_total}s\n" \
                --max-time 35 \
                -X POST "${API_BASE}/api/orders" \
                -H "Content-Type: application/json" \
                -d "{\"customerName\":\"p1-noisy-${round}-${i}\",\"customerEmail\":\"p1-${round}-${i}@test.com\",\"items\":[{\"productId\":$(( i % 16 + 1 )),\"quantity\":1}]}" &
        done
        wait
    ' >> "$RESULT_LOG" 2>&1
}

cleanup() {
    if [[ -f "$PID_FILE" ]]; then
        kill "$(cat "$PID_FILE")" 2>/dev/null || true
        rm -f "$PID_FILE"
    fi
    # 잔여 stress-ng 워커 정리 (timeout 만료 전 강제 종료 대비)
    pkill -f "stress-ng --cpu" 2>/dev/null || true
    rm -f "$RESULT_LOG"
    log_info "INJECTION_END_MS=$(date +%s%3N)"
    log_ok "Cleanup complete (noisy-neighbor stress-ng terminated)"
}

main() {
    if [[ "${1:-}" == "cleanup" ]]; then cleanup; trap - EXIT; exit 0; fi
    echo "============================================================"
    echo "  P-1 plopvape: Noisy-Neighbor Host CPU Saturation"
    echo "  Start: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "============================================================"
    rm -f "$RESULT_LOG"
    trap cleanup EXIT
    check_prerequisites
    saturate_cpu
    # 부하가 끝날 때까지 order를 분산 주입(부하 영향 관측). main은 부하 완주를 대기한다.
    local stress_pid round=1
    stress_pid=$(cat "$PID_FILE" 2>/dev/null || echo "")
    while [[ -n "$stress_pid" ]] && kill -0 "$stress_pid" 2>/dev/null; do
        send_orders "$round"
        log_info "round ${round} sent (load holding)"
        round=$((round + 1))
        sleep "$ROUND_SLEEP_SEC"
    done
    wait "$stress_pid" 2>/dev/null || true
    log_info "load window complete (~${LOAD_DURATION_SEC}s)"
    [[ -f "$RESULT_LOG" ]] && tail -n 30 "$RESULT_LOG"
}

main "$@"
