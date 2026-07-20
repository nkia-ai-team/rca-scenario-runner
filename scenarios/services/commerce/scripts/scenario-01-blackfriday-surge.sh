#!/usr/bin/env bash
# =============================================================================
# commerce 시나리오 01: Black Friday Surge (L1) — 실측 80 RPS latency brownout
# =============================================================================
# testbed-services docs/spec-scenario-load.md 규칙(R0~R9)의 첫 실전 적용.
# 기존 plopvape-shop scenario-04(절대 동시성 5→500, k3d exec 주입)의 개정판이다.
#
# Root Cause: 인입 surge 80 RPS(실측 baseline 포함 총 약 88 RPS)
#             → order 처리 용량 무릎 초과 → HikariCP pending·latency·drop 증가.
#
# 주입(R2): tb-runner VM(122.206)에서 k6로, baseline과 같은 진입 경로
#           (tb-cp NodePort 30080)로 north-south 주입. surge 스크립트 정본은
#           testbed-services commerce/loadgen/surge.js — tb-runner
#           /opt/loadgen/commerce/surge.js 로 배치돼 있어야 한다(사전 조건).
# 강도(R4-1): capacity probe3에서 실제 관측한 surge 80 RPS를 정본으로 쓴다.
#           100+ RPS 완전 pool 고갈은 미실측이므로 기대하지 않는다.
# 생명주기(R3): k6는 ramp-up/hold/ramp-down이 끝나면 스스로 종료. cleanup이
#           잔존 프로세스를 확인 사살하고 baseline loadgen 무사도 확인(R1).
#
# 사용법:
#   ./scenario-01-blackfriday-surge.sh          # 실행
#   ./scenario-01-blackfriday-surge.sh cleanup  # 잔존 surge 종료 + 상태 확인
#   ./scenario-01-blackfriday-surge.sh dry-run  # 무접속 실행 계획 출력
#
# 실행 환경: 109 호스트(root) — /root/.ssh/tb_key 로 tb-runner ssh 가능해야 함.
# =============================================================================

set -uo pipefail

# --- 설정 (전부 env 오버라이드 가능) ---
SSH_KEY="${SSH_KEY:-/root/.ssh/tb_key}"
RUNNER="${RUNNER:-nkia@192.168.122.206}"
ENTRY="${ENTRY:-http://192.168.122.77:30080}"          # tb-cp NodePort — worker 장애에 안 휘말리는 진입점
export KUBECONFIG="${KUBECONFIG:-/root/tb-kubeconfig}"
NAMESPACE="${NAMESPACE:-rca-testbed-commerce}"
PG_POD="${PG_POD:-testbed-postgres-0}"

TARGET_RPS="${TARGET_RPS:-80}"                         # probe3 실측 상한(정본 강도)
REFERENCE_BASELINE_RPS="${REFERENCE_BASELINE_RPS:-8}" # probe3 당시 baseline
RAMP_UP="${RAMP_UP:-2m}"
HOLD="${HOLD:-8m}"
RAMP_DOWN="${RAMP_DOWN:-1m}"
SURGE_SEED="${SURGE_SEED:-4242}"
PEAK_RPS="${PEAK_RPS:-10}"                             # baseline loadgen unit과 같은 값이어야 함
TROUGH_RPS="${TROUGH_RPS:-2}"
MIN_FLAGSHIP_STOCK="${MIN_FLAGSHIP_STOCK:-900000}"     # 100만 seed에서 baseline 감소 여유
ABORT_LATENCY_SEC="${ABORT_LATENCY_SEC:-10}"
ABORT_CONSECUTIVE="${ABORT_CONSECUTIVE:-3}"
RUN_META="${RUN_META:-/tmp/scenario-01-blackfriday-surge-meta.json}"

SURGE_SCRIPT="/opt/loadgen/commerce/surge.js"          # tb-runner 상 경로 (loadgen 배치 절차로 설치)
SURGE_TAG="loadgen/commerce/surge.js"                  # pkill 매칭 — baseline(script.js)과 절대 겹치지 않게

CYAN='\033[0;36m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log_info()  { echo -e "${CYAN}[INFO]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}   $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }

rssh() { ssh -i "$SSH_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$RUNNER" "$@"; }

# --- baseline RPS 계산 (loadgen entrypoint.sh의 diurnal 테이블과 동일) ---
diurnal_pct() {
    case "$1" in
        0) echo 25 ;;  1) echo 15 ;;  2) echo 7 ;;   3) echo 2 ;;
        4) echo 0 ;;   5) echo 2 ;;   6) echo 7 ;;   7) echo 15 ;;
        8) echo 25 ;;  9) echo 37 ;; 10) echo 50 ;;  11) echo 63 ;;
        12) echo 75 ;; 13) echo 85 ;; 14) echo 93 ;; 15) echo 98 ;;
        16) echo 100 ;; 17) echo 98 ;; 18) echo 93 ;; 19) echo 85 ;;
        20) echo 75 ;; 21) echo 63 ;; 22) echo 50 ;; 23) echo 37 ;;
        *) echo 50 ;;
    esac
}

baseline_rps() {
    local hour pct
    hour=$(TZ=Asia/Seoul date +%H); hour=${hour#0}; hour=${hour:-0}
    pct=$(diurnal_pct "$hour")
    echo $(( TROUGH_RPS + ( (PEAK_RPS - TROUGH_RPS) * pct + 50 ) / 100 ))
}

# "2m"/"30s" → 초
to_secs() {
    local v=$1
    case "$v" in
        *m) echo $(( ${v%m} * 60 )) ;;
        *s) echo "${v%s}" ;;
        *)  echo "$v" ;;
    esac
}

# --- 스냅샷: DB 세션 + 진입점 응답시간 (증상 발현 추적용 로그) ---
LAST_HTTP_CODE="000"
LAST_LATENCY_SEC="999"
take_snapshot() {
    local sessions probe
    sessions=$(kubectl -n "$NAMESPACE" exec "$PG_POD" -- \
        psql -U commerce -d commerce -t -A \
        -c "SELECT count(*) || ' (active:' || count(*) FILTER (WHERE state='active') || ')' FROM pg_stat_activity WHERE datname='commerce';" 2>/dev/null || echo "n/a")
    probe=$(curl -s -o /dev/null -w "%{time_total} %{http_code}" --max-time 20 \
        "$ENTRY/api/products?size=5" 2>/dev/null || echo "999 000")
    read -r LAST_LATENCY_SEC LAST_HTTP_CODE <<<"$probe"
    log_info "  [snapshot] DB sessions=${sessions}  entry latency=${LAST_LATENCY_SEC}s (HTTP ${LAST_HTTP_CODE})"
}

snapshot_exceeds_stop_condition() {
    [[ "$LAST_HTTP_CODE" == "200" ]] || return 0
    awk -v observed="$LAST_LATENCY_SEC" -v limit="$ABORT_LATENCY_SEC" \
        'BEGIN { exit !(observed > limit) }'
}

# --- 사전 조건 (R1 포함) ---
check_prerequisites() {
    log_info "사전 조건 확인 중..."

    if ! rssh "test -f $SURGE_SCRIPT"; then
        log_error "tb-runner에 $SURGE_SCRIPT 없음 — testbed-services commerce/loadgen/surge.js 배치 필요"
        exit 1
    fi

    local lg_state
    lg_state=$(rssh "systemctl is-active loadgen-commerce" 2>/dev/null || echo "unknown")
    if [[ "$lg_state" != "active" ]]; then
        log_error "baseline loadgen-commerce가 active가 아님($lg_state) — R1 전제 불충족, 중단"
        exit 1
    fi
    log_ok "baseline loadgen-commerce active (건드리지 않음 — R1)"

    if rssh "pgrep -f '$SURGE_TAG'" >/dev/null 2>&1; then
        log_error "이전 surge k6가 아직 살아있음 — cleanup 먼저 실행 필요"
        exit 1
    fi

    [[ "$TARGET_RPS" =~ ^[0-9]+$ ]] || { log_error "TARGET_RPS는 정수여야 함"; exit 1; }

    local stock_state flagship_count min_stock
    stock_state=$(kubectl -n "$NAMESPACE" exec "$PG_POD" -- \
        psql -U commerce -d commerce -tA -F ' ' -c \
        "SELECT count(*), min(stock) FROM inventory_schema.inventory WHERE product_id BETWEEN 1 AND 16;" \
        2>/dev/null || echo "0 0")
    read -r flagship_count min_stock <<<"$stock_state"
    if [[ "$flagship_count" != "16" || ! "$min_stock" =~ ^[0-9]+$ || "$min_stock" -lt "$MIN_FLAGSHIP_STOCK" ]]; then
        log_error "flagship 재고 precondition 실패: rows=$flagship_count min_stock=$min_stock (필요 >=$MIN_FLAGSHIP_STOCK)"
        log_error "스크립트는 baseline 데이터를 수정하지 않으므로 재고 준비 후 다시 실행"
        exit 1
    fi
    log_ok "flagship 재고 precondition 통과 (rows=16 min_stock=$min_stock)"

    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$ENTRY/health" 2>/dev/null || echo "000")
    if [[ "$code" != "200" ]]; then
        log_error "진입점($ENTRY) 접근 불가 (HTTP $code)"
        exit 1
    fi
    log_ok "사전 조건 확인 완료"
}

# --- 원상복구 (R3: 종료 보증 / R1: baseline 무사 확인) ---
cleanup() {
    log_info "=== cleanup 시작 ==="

    if rssh "pgrep -f '$SURGE_TAG'" >/dev/null 2>&1; then
        log_warn "잔존 surge k6 발견 — 강제 종료"
        rssh "pkill -f '$SURGE_TAG'" || true
        sleep 2
    fi
    if rssh "pgrep -f '$SURGE_TAG'" >/dev/null 2>&1; then
        log_error "surge k6가 여전히 살아있음 — 수동 확인 필요"
    else
        log_ok "surge k6 없음 확인 (baseline은 별도 프로세스라 매칭 안 됨)"
    fi

    local lg_state
    lg_state=$(rssh "systemctl is-active loadgen-commerce" 2>/dev/null || echo "unknown")
    if [[ "$lg_state" == "active" ]]; then
        log_ok "baseline loadgen-commerce 여전히 active (R1 유지)"
    else
        log_error "baseline loadgen-commerce 상태 이상: $lg_state — 확인 필요"
    fi

    rssh "rm -f /tmp/surge-l1.pid" || true
    log_ok "=== cleanup 완료 ==="
}

main() {
    local base_rps total_secs display_multiplier t1 t2
    base_rps=$(baseline_rps)
    display_multiplier=$(awk -v surge="$TARGET_RPS" -v base="$base_rps" \
        'BEGIN { if (base > 0) printf "%.1f", surge/base; else print "n/a" }')
    total_secs=$(( $(to_secs "$RAMP_UP") + $(to_secs "$HOLD") + $(to_secs "$RAMP_DOWN") ))

    echo ""
    echo "╔═══════════════════════════════════════════════════════════╗"
    echo "║  commerce 시나리오 01: Black Friday Surge (L1)             ║"
    echo "║  surge ${TARGET_RPS} rps + 현재 baseline 약 ${base_rps} rps (${display_multiplier}배)"
    echo "║  ramp ${RAMP_UP} → hold ${HOLD} → down ${RAMP_DOWN} (총 ${total_secs}s)"
    echo "║  시작: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "╚═══════════════════════════════════════════════════════════╝"
    echo ""

    trap cleanup EXIT
    check_prerequisites
    take_snapshot

    log_info "surge 주입 시작 (tb-runner에서 k6 실행)"
    t1=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
    log_info "  t1=$t1 — golden 시간 기준점"
    rssh "nohup k6 run \
        --env GATEWAY_URL='$ENTRY' \
        --env TARGET_RPS='$TARGET_RPS' \
        --env RAMP_UP='$RAMP_UP' --env HOLD='$HOLD' --env RAMP_DOWN='$RAMP_DOWN' \
        --env SURGE_SEED='$SURGE_SEED' \
        --summary-export /tmp/surge-l1-summary.json \
        $SURGE_SCRIPT > /tmp/surge-l1.log 2>&1 & echo \$! > /tmp/surge-l1.pid"
    log_ok "k6 기동됨 (pid=$(rssh 'cat /tmp/surge-l1.pid' 2>/dev/null || echo '?'))"

    # 진행 관찰 — 30초 간격 스냅샷 (증상 발현 시각을 로그에 남긴다)
    local elapsed=0 bad_snapshots=0 aborted=0
    while (( elapsed < total_secs + 30 )); do
        sleep 30; elapsed=$(( elapsed + 30 ))
        log_info "경과 ${elapsed}/${total_secs}s"
        take_snapshot
        if snapshot_exceeds_stop_condition; then
            bad_snapshots=$(( bad_snapshots + 1 ))
            log_warn "중단 조건 관측 ${bad_snapshots}/${ABORT_CONSECUTIVE} (HTTP=$LAST_HTTP_CODE latency=${LAST_LATENCY_SEC}s)"
        else
            bad_snapshots=0
        fi
        if (( bad_snapshots >= ABORT_CONSECUTIVE )); then
            log_error "전면 장애 방지 중단 조건 도달 — surge 종료"
            rssh "test -f /tmp/surge-l1.pid && kill \$(cat /tmp/surge-l1.pid)" 2>/dev/null || true
            aborted=1
            break
        fi
        if ! rssh "pgrep -f '$SURGE_TAG'" >/dev/null 2>&1; then
            log_info "k6 종료 감지 (duration 만료)"
            break
        fi
    done

    # 결과 요약
    echo ""
    log_info "=== k6 결과 요약 ==="
    rssh "tail -20 /tmp/surge-l1.log" 2>/dev/null || log_warn "k6 로그 조회 실패"
    local achieved_rps="n/a"
    achieved_rps=$(rssh "cat /tmp/surge-l1-summary.json" 2>/dev/null | \
        python -c 'import json,sys; d=json.load(sys.stdin); print(d.get("metrics",{}).get("iterations",{}).get("values",{}).get("rate","n/a"))' \
        2>/dev/null || echo "n/a")
    log_info "target_rps=$TARGET_RPS achieved_iteration_rps=$achieved_rps"
    echo ""
    t2=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
    log_info "t2=$t2 — 이후 회복 구간(관측은 계속 baseline 위에서)"
    printf '{"scenario_id":"commerce/scenario-01","t1":"%s","t2":"%s","target_rps":%s,"achieved_iteration_rps":"%s","aborted":%s}\n' \
        "$t1" "$t2" "$TARGET_RPS" "$achieved_rps" "$([[ "$aborted" == "1" ]] && echo true || echo false)" > "$RUN_META"
    take_snapshot

    if (( aborted == 1 )); then
        log_error "시나리오가 안전 중단됨 — golden 승격/캡처 금지"
        return 1
    fi

    echo ""
    echo "============================================================"
    echo "  시나리오 01 (L1) 완료 — 종료: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "  검증: expected_anomalies가 관측됐는지 service-spec 참조"
    echo "============================================================"
}

print_dry_run() {
    cat <<EOF
scenario=commerce/scenario-01
side_effects=false
orchestrator=scenario-runner@192.168.200.109
injection_kind=north_south
injection_location=tb-runner@192.168.122.206
transport=ssh
entry_path=tb-runner -> ${ENTRY} -> commerce api-gateway
target_rps=${TARGET_RPS}
cleanup_location=tb-runner@192.168.122.206
required_checks=ssh-key,known-host,k6,surge-script,baseline-unit,nodeport-health,inventory-precondition
EOF
}

case "${1:-run}" in
    cleanup)
        trap - EXIT
        cleanup
        ;;
    dry-run)
        print_dry_run
        ;;
    run)
        main
        ;;
    *)
        log_error "unsupported mode: $1 (use run|cleanup|dry-run)"
        exit 2
        ;;
esac
