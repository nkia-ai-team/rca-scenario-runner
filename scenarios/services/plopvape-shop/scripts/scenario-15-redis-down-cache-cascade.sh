#!/usr/bin/env bash
# =============================================================================
# RT-3 / plopvape: Redis(캐시) 다운 → 캐시 의존 서비스 캐스케이드 5xx
# =============================================================================
# Root Cause: 캐시(redis) 파드를 내려(replicas=0) 캐시 계층을 제거한다. redis에
#             하드 의존하는 order 경로가 캐시 접근 실패로 전부 5xx(502)로 떨어진다.
#             DB 락/연결 문제가 아니라 캐시 인프라 부재가 원인이다.
#
# 전파 경로: redis down → order 서비스 캐시 접근 실패 → nginx upstream 5xx(502)
#            (product 경로는 redis 비의존/fallback이라 200 유지 — 영향 범위 변별점)
#
# 관측 증거(2026-06-05 실측):
#   - apm order 경로 5xx: POST /api/orders 가 502 (baseline 200/409 → 깨끗한 flip,
#     1차 신호). GET /api/orders 도 502이나 baseline 이 선재 flaky(~50% 502)라 부차.
#   - kcm event(Warning): order 서비스의 actuator/health 가 redis 헬스를 포함 → redis
#     다운 시 liveness/readiness 실패 → order 파드 `Unhealthy`+`BackOff` Warning 발생.
#     kcm collector 는 type=Warning 만 surface 하므로 이 의존-파드 Warning 이 kcm leg.
#     (redis scale 이벤트 자체는 Normal 이라 kcm 미surface.)
#   - product 경로(GET /api/products)는 redis 비의존이라 200 — 영향이 캐시 의존
#     경로에 국한됨을 보이는 변별 신호.
#
# 회복 주의: redis 다운이 order 를 CrashLoopBackOff 로 몰아 redis 복원 후에도 ~100s
#   회복 지연 → cleanup 에서 의존 서비스(DEP_SERVICES)를 rollout 재시작·Ready 대기.
#
#   ./scenario-15-redis-down-cache-cascade.sh           # 실행
#   ./scenario-15-redis-down-cache-cascade.sh cleanup   # 원상복구(redis 복원)
# =============================================================================

set -uo pipefail

export KUBECONFIG=/home/nkia/.kube/plopvape.yaml
NAMESPACE="${NAMESPACE:-rca-testbed-plopvape}"
REDIS_DEPLOY="${REDIS_DEPLOY:-testbed-redis}"
# redis 헬스에 의존해 fault 중 CrashLoop 에 빠지는 서비스(cleanup 시 rollout 재시작 대상).
DEP_SERVICES="${DEP_SERVICES:-testbed-order}"
# k3d: 호스트 NodePort/노드 IP(172.x) 는 미노출·취약 → 클러스터 내부 nginx 를 앱 파드 exec 로 호출.
# redis 다운이 order 파드를 CrashLoop 로 몰므로 exec 주체는 redis 비의존(=안정) 인 product 파드.
API_BASE="${API_BASE:-http://testbed-nginx-external}"
APP_POD_LABEL="${APP_POD_LABEL:-app=testbed-product}"
LOAD_DURATION_SEC="${LOAD_DURATION_SEC:-300}"
CONCURRENT_REQ="${CONCURRENT_REQ:-15}"
ROUND_SLEEP_SEC="${ROUND_SLEEP_SEC:-4}"
RESULT_LOG="/tmp/rt3-plopvape-redis-down.log"

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
api_curl() { kubectl -n "$NAMESPACE" exec "$API_POD" -- curl "$@"; }

check_prerequisites() {
    if ! kubectl -n "$NAMESPACE" get deploy "$REDIS_DEPLOY" &>/dev/null; then
        log_error "redis deploy not found: $REDIS_DEPLOY"
        exit 1
    fi
    resolve_api_pod
    if [[ -z "$API_POD" ]]; then
        log_error "load-gen 파드($APP_POD_LABEL) 없음 — API 호출 불가"
        exit 1
    fi
    local code
    code=$(api_curl -s -o /dev/null -w "%{http_code}" --max-time 8 "$API_BASE/api/orders" 2>/dev/null || echo "000")
    log_info "redis deploy=$REDIS_DEPLOY, API /api/orders baseline=$code (via $API_POD)"
}

inject_redis_down() {
    echo "INJECTION_START_MS=$(date +%s%3N)"
    log_info "=== redis 다운 주입: $REDIS_DEPLOY replicas 0 ==="
    kubectl -n "$NAMESPACE" scale deploy "$REDIS_DEPLOY" --replicas=0 >/dev/null 2>&1
    local waited=0
    while [[ $waited -lt 20 ]]; do
        local n
        n=$(kubectl -n "$NAMESPACE" get pods -l app="$REDIS_DEPLOY" --no-headers 2>/dev/null | wc -l)
        [[ "$n" == "0" ]] && break
        sleep 1; waited=$((waited + 1))
    done
    log_ok "redis 파드 제거 완료 (${waited}s)"
}

send_load() {
    local round=$1
    local pids=()
    for i in $(seq 1 "$CONCURRENT_REQ"); do
        # 캐시 의존 경로(order) 에 부하 → redis down 시 502
        api_curl -s -o /dev/null -w "rt3-getord-${round}-${i}: HTTP %{http_code}\n" --max-time 20 \
            "$API_BASE/api/orders" >> "$RESULT_LOG" 2>&1 &
        pids+=($!)
        api_curl -s -o /dev/null -w "rt3-postord-${round}-${i}: HTTP %{http_code}\n" --max-time 20 \
            -X POST "$API_BASE/api/orders" -H "Content-Type: application/json" \
            -d "{\"customerName\":\"rt3-${round}-${i}\",\"customerEmail\":\"rt3-${round}-${i}@test.com\",\"items\":[{\"productId\":$((i % 16 + 1)),\"quantity\":1}]}" \
            >> "$RESULT_LOG" 2>&1 &
        pids+=($!)
    done
    for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
}

cleanup() {
    log_info "=== cleanup: redis 복원 ==="
    kubectl -n "$NAMESPACE" scale deploy "$REDIS_DEPLOY" --replicas=1 >/dev/null 2>&1 || true
    local waited=0
    while [[ $waited -lt 60 ]]; do
        local ready
        ready=$(kubectl -n "$NAMESPACE" get pods -l app="$REDIS_DEPLOY" --no-headers 2>/dev/null | grep -c "1/1" || true)
        [[ "$ready" == "1" ]] && { log_ok "redis 복원 완료 (${waited}s)"; break; }
        sleep 2; waited=$((waited + 2))
    done
    # redis 헬스에 의존하는 서비스(order 등)는 fault 중 liveness 실패로 CrashLoopBackOff에
    # 빠진다. redis 복원만으론 BackOff 백오프 때문에 회복이 느리므로, 의존 서비스를 rollout
    # 재시작해 깨끗하게 되살리고 Ready 까지 대기한다(테스트베드 잔존 손상 방지).
    for dep in $DEP_SERVICES; do
        kubectl -n "$NAMESPACE" rollout restart deploy "$dep" >/dev/null 2>&1 || true
    done
    for dep in $DEP_SERVICES; do
        kubectl -n "$NAMESPACE" rollout status deploy "$dep" --timeout=180s >/dev/null 2>&1 \
            && log_ok "$dep Ready" || log_error "$dep 회복 대기 타임아웃 — 수동 확인 필요"
    done
    rm -f "$RESULT_LOG"
    echo "INJECTION_END_MS=$(date +%s%3N)"
    log_ok "Cleanup complete"
}

main() {
    if [[ "${1:-}" == "cleanup" ]]; then
        cleanup
        trap - EXIT
        exit 0
    fi
    echo "============================================================"
    echo "  RT-3 plopvape: Redis Down → Cache Cascade 5xx"
    echo "  Start: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "============================================================"
    rm -f "$RESULT_LOG"
    trap cleanup EXIT
    check_prerequisites
    inject_redis_down

    local end_time=$(( $(date +%s) + LOAD_DURATION_SEC ))
    local round=1
    while [[ $(date +%s) -lt $end_time ]]; do
        send_load "$round"
        log_info "round ${round} 전송 (redis down 유지 중)"
        round=$((round + 1))
        sleep "$ROUND_SLEEP_SEC"
    done
    log_info "load window 종료 (~${LOAD_DURATION_SEC}s)"

    if [[ -f "$RESULT_LOG" ]]; then
        echo "--- HTTP code 분포 ---"
        grep -oE 'HTTP [0-9]+' "$RESULT_LOG" | sort | uniq -c || true
    fi
}

main "$@"
