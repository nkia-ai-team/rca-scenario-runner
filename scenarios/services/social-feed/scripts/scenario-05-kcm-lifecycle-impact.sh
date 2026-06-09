#!/usr/bin/env bash
# =============================================================================
# G-5 / social-feed: KCM Lifecycle With Service Impact
# =============================================================================
# Root Cause: feed-service deployment를 0 replicas로 낮춰 KCM lifecycle event와
#             user-facing feed impact를 함께 만든다.
#
# Usage:
#   ./scenario-05-kcm-lifecycle-impact.sh
#   ./scenario-05-kcm-lifecycle-impact.sh cleanup
# =============================================================================

set -uo pipefail

# k3d 도메인별 클러스터: social 전용 kubeconfig (공유 config 의 current-context 드리프트 무관)
export KUBECONFIG=/home/nkia/.kube/social.yaml
NAMESPACE="rca-testbed-social"
TARGET_DEPLOY="testbed-feed"
APP_POD_LABEL="app=testbed-post"
# k3d 는 호스트로 NodePort 를 publish 하지 않으므로, API 호출은 클러스터 내부에서
# 앱 파드(curl 보유) 를 kubectl exec 경유로 nginx 게이트웨이로 보낸다.
API_BASE="http://testbed-nginx-external"
ORIG_REPLICAS_FILE="/tmp/g5-social-feed-orig-replicas"
RESULT_LOG="/tmp/g5-social-kcm-impact-results.log"
REQUEST_ROUNDS=5
CONCURRENT_REQUESTS=20

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
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
    log_info "Checking prerequisites"
    if ! kubectl -n "$NAMESPACE" get deploy "$TARGET_DEPLOY" &>/dev/null; then
        log_error "deployment not found: $TARGET_DEPLOY"
        exit 1
    fi
    resolve_api_pod
    if [[ -z "$API_POD" ]]; then
        log_error "post-service Pod 없음 (API 호출 불가)"
        exit 1
    fi
    local code
    code=$(api_curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$API_BASE/actuator/health" 2>/dev/null || echo "000")
    log_info "nginx/root health probe HTTP $code"
    log_ok "Prerequisites checked"
}

induce_kcm_impact() {
    log_info "INJECTION_START_MS=$(date +%s%3N)"
    kubectl -n "$NAMESPACE" get deploy "$TARGET_DEPLOY" -o jsonpath='{.spec.replicas}' > "$ORIG_REPLICAS_FILE"
    kubectl -n "$NAMESPACE" scale deploy "$TARGET_DEPLOY" --replicas=0
    sleep 10
    kubectl -n "$NAMESPACE" get deploy "$TARGET_DEPLOY" -o wide || true
    log_ok "Scaled $TARGET_DEPLOY to 0 replicas"
}

send_feed_requests() {
    local round=$1
    log_info "Round $round: feed endpoint requests while feed deployment is unavailable"
    local pids=()
    for i in $(seq 1 "$CONCURRENT_REQUESTS"); do
        local user_id=$((i % 10 + 1))
        api_curl -s -o /dev/null \
            -w "g5-feed-${round}-${i}: HTTP %{http_code} in %{time_total}s\n" \
            --max-time 15 \
            "$API_BASE/api/feed/${user_id}" \
            >> "$RESULT_LOG" 2>&1 &
        pids+=($!)
    done
    for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
}

analyze_results() {
    if [[ ! -f "$RESULT_LOG" ]]; then
        log_warn "No result log"
        return
    fi
    local total errors avg_time
    total=$(wc -l < "$RESULT_LOG")
    errors=$(grep -c "HTTP [45]" "$RESULT_LOG" 2>/dev/null || echo "0")
    avg_time=$(awk -F'in ' '{print $2}' "$RESULT_LOG" | awk -F's' '{s+=$1; c++} END {if(c>0) printf "%.2f", s/c; else print "N/A"}')
    echo "total=$total errors=$errors avg=${avg_time}s"
    tail -n 20 "$RESULT_LOG"
}

cleanup() {
    log_info "Cleanup start"
    local orig=1
    if [[ -f "$ORIG_REPLICAS_FILE" ]]; then
        orig=$(cat "$ORIG_REPLICAS_FILE")
    fi
    kubectl -n "$NAMESPACE" scale deploy "$TARGET_DEPLOY" --replicas="$orig" 2>/dev/null || true
    kubectl -n "$NAMESPACE" rollout status deploy "$TARGET_DEPLOY" --timeout=120s 2>/dev/null || true
    rm -f "$ORIG_REPLICAS_FILE" "$RESULT_LOG"
    log_info "INJECTION_END_MS=$(date +%s%3N)"
    log_ok "Cleanup complete (replicas=$orig)"
}

main() {
    if [[ "${1:-}" == "cleanup" ]]; then
        cleanup
        trap - EXIT
        exit 0
    fi
    echo "============================================================"
    echo "  G-5 social-feed: KCM Lifecycle With Service Impact"
    echo "  Start: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "============================================================"
    rm -f "$RESULT_LOG"
    trap cleanup EXIT
    check_prerequisites
    induce_kcm_impact
    for round in $(seq 1 "$REQUEST_ROUNDS"); do
        send_feed_requests "$round"
        sleep 8
    done
    analyze_results
}

main "$@"
