#!/usr/bin/env bash
# =============================================================================
# P-4 / plopvape-shop: Process CPU Spike Without Service Impact (NEGATIVE)
# =============================================================================
# Role: negative guardrail. 프로세스 CPU가 짧게 튀어도 서비스 영향(RT/error floor)이
#       없으면 RCA가 그 프로세스를 root로 과확정하면 안 된다. P-1~P-3 root eligibility
#       완화 후 false positive를 검출하는 쌍이다.
#
# 의도: 짧은(기본 20s) 프로세스 CPU spike만 만들고, 지속 부하나 서비스 영향은 만들지
#       않는다. APM 서비스 RT/error는 정상 floor를 유지해야 한다.
#
# Expected RCA: INCONCLUSIVE / NEEDS_MORE_EVIDENCE. 프로세스 spike만으로 CONCLUSIVE
#               root를 내면 fail.
#
# NOTE(주입 검증): cleanup/판정의 핵심은 "spike 구간에 정렬된 서비스 impact floor가
#   없음"이다. 109 주입 시 APM 서비스 RT/error가 평시 수준을 유지하는지 확인한다.
#
#   ./scenario-13-process-spike-no-impact.sh           # 시나리오 실행
#   ./scenario-13-process-spike-no-impact.sh cleanup   # 원상복구
# =============================================================================

set -uo pipefail

# 약한 spike: 코어 일부만. 프로세스 메트릭엔 1포인트 잡히되 서비스 영향은 만들지 않는 것이 목적.
SPIKE_WORKERS="${SPIKE_WORKERS:-2}"
SPIKE_DURATION_SEC="${SPIKE_DURATION_SEC:-90}"
PID_FILE="/tmp/p4-plopvape-process-spike.pid"
RESULT_LOG="/tmp/p4-plopvape-process-spike.log"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
log_info()  { echo -e "${CYAN}[INFO]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}   $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }

check_prerequisites() {
    if ! command -v stress-ng &>/dev/null; then
        log_error "stress-ng not found. 설치: sudo yum install stress-ng (RHEL) / sudo apt install stress-ng (Debian)"
        exit 1
    fi
    log_info "stress-ng available; SPIKE_WORKERS=$SPIKE_WORKERS SPIKE_DURATION=${SPIKE_DURATION_SEC}s (no service load)"
}

short_spike() {
    log_info "INJECTION_START_MS=$(date +%s%3N)"
    # 짧은 spike. 서비스 부하는 의도적으로 보내지 않는다(impact floor 없음 보장).
    stress-ng --cpu "$SPIKE_WORKERS" --timeout "${SPIKE_DURATION_SEC}s" --metrics-brief \
        >> "$RESULT_LOG" 2>&1 &
    echo $! > "$PID_FILE"
    log_info "short process spike started (pid=$(cat "$PID_FILE"), ${SPIKE_DURATION_SEC}s)"
    wait "$(cat "$PID_FILE")" 2>/dev/null || true
    log_info "spike finished; service load intentionally not generated"
}

cleanup() {
    if [[ -f "$PID_FILE" ]]; then
        kill "$(cat "$PID_FILE")" 2>/dev/null || true
        rm -f "$PID_FILE"
    fi
    pkill -f "stress-ng --cpu" 2>/dev/null || true
    rm -f "$RESULT_LOG"
    log_info "INJECTION_END_MS=$(date +%s%3N)"
    log_ok "Cleanup complete (short spike terminated)"
}

main() {
    if [[ "${1:-}" == "cleanup" ]]; then cleanup; trap - EXIT; exit 0; fi
    echo "============================================================"
    echo "  P-4 plopvape: Process CPU Spike Without Service Impact (NEGATIVE)"
    echo "  Start: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "============================================================"
    rm -f "$RESULT_LOG"
    trap cleanup EXIT
    check_prerequisites
    short_spike
    [[ -f "$RESULT_LOG" ]] && tail -n 30 "$RESULT_LOG"
}

main "$@"
