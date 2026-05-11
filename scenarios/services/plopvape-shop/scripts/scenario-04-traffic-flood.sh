#!/usr/bin/env bash
# =============================================================================
# 시나리오 04: 블랙프라이데이 트래픽 폭주 → 전체 서비스 캐스케이드 장애
# =============================================================================
# Root Cause: 평소 대비 수십 배의 동시 주문이 발생하여 서비스의 처리 용량
#             (HikariCP 커넥션 풀, Tomcat 스레드 풀)을 초과한다.
#             인위적 주입 없이 순수한 트래픽만으로 자연스러운 장애 발생.
#
# 전파 경로: 동시 주문 폭주 → DB 커넥션 풀 포화 → 스레드 풀 포화 → 타임아웃 → 5xx
#
# 사용법:
#   ./scenario-04-traffic-flood.sh          # 시나리오 실행
#   ./scenario-04-traffic-flood.sh cleanup  # 원상복구 (재고 복원)
#
# 실행 환경: K3s + plopvape-shop 이 배포된 호스트 (rca-scenario-runner 가 같은 호스트에 떠있음)
# =============================================================================

set -uo pipefail

# --- 설정 ---
export KUBECONFIG=/home/nkia/.kube/config
NAMESPACE="${NAMESPACE:-rca-testbed-plopvape}"
PG_POD="${PG_POD:-testbed-postgres-0}"
API_BASE="${API_BASE:-http://127.0.0.1:30080}"

# 트래픽 단계 (점진적 증가)
PHASE_1_CONCURRENT=5            # 정상 트래픽
PHASE_2_CONCURRENT=50           # 증가 트래픽
PHASE_3_CONCURRENT=200          # 폭주 트래픽
PHASE_4_CONCURRENT=500          # 극단적 폭주

# --- 색상 ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info()  { echo -e "${CYAN}[INFO]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}   $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }

# --- 사전 조건 확인 ---
check_prerequisites() {
    log_info "사전 조건 확인 중..."

    if ! kubectl -n "$NAMESPACE" get pod "$PG_POD" &>/dev/null; then
        log_error "PostgreSQL Pod 접근 불가"
        exit 1
    fi

    local http_code
    http_code=$(curl -s -o /dev/null -w "%{http_code}" "$API_BASE/api/products" 2>/dev/null || echo "000")
    if [[ "$http_code" != "200" ]]; then
        log_error "API 접근 불가 (HTTP $http_code)"
        exit 1
    fi

    # 재고를 넉넉하게 보충 (트래픽 폭주 테스트이므로 재고 부족으로 실패하면 안 됨)
    kubectl -n "$NAMESPACE" exec "$PG_POD" -- \
        psql -U plopvape -d plopvape \
        -c "UPDATE inventory_schema.inventory SET stock = 9999;" 2>/dev/null
    log_ok "재고를 9999로 설정 (재고 부족이 아닌 트래픽 포화를 테스트)"

    log_ok "사전 조건 확인 완료"
}

# --- 동시 주문 발생 + 결과 수집 ---
send_orders() {
    local phase=$1
    local concurrent=$2
    local label=$3

    log_info "====================================================="
    log_info "  Phase $phase: $label ($concurrent건 동시 주문)"
    log_info "====================================================="

    local pids=()
    local start_time
    start_time=$(date +%s)

    # 다양한 상품에 대해 주문 (hot product + 분산)
    for i in $(seq 1 "$concurrent"); do
        local product_id
        if (( i % 2 == 0 )); then
            product_id=1  # 인기 상품 (50%가 동일 상품 집중 → row lock contention)
        else
            product_id=$(( (i % 16) + 1 ))  # 나머지 분산
        fi

        curl -s -o /dev/null \
            -w "phase${phase}-order-${i}: HTTP %{http_code} in %{time_total}s\n" \
            --max-time 35 \
            -X POST "$API_BASE/api/orders" \
            -H "Content-Type: application/json" \
            -d "{\"customerName\":\"flood-p${phase}-${i}\",\"customerEmail\":\"flood${i}@test.com\",\"items\":[{\"productId\":${product_id},\"quantity\":1}]}" \
            >> /tmp/scenario-04-results.log 2>&1 &
        pids+=($!)
    done

    log_info "  $concurrent건 전송 완료, 응답 대기 중..."

    # 완료 대기
    for pid in "${pids[@]}"; do
        wait "$pid" 2>/dev/null || true
    done

    local end_time
    end_time=$(date +%s)
    local elapsed=$(( end_time - start_time ))

    # 이 Phase 결과 분석
    local phase_total phase_errors phase_avg phase_max phase_slow
    phase_total=$(grep "^phase${phase}-" /tmp/scenario-04-results.log 2>/dev/null | wc -l)
    phase_errors=$(grep "^phase${phase}-" /tmp/scenario-04-results.log 2>/dev/null | grep "HTTP [45]" | wc -l)
    phase_avg=$(grep "^phase${phase}-" /tmp/scenario-04-results.log 2>/dev/null | awk -F'in ' '{print $2}' | awk -F's' '{sum+=$1; count++} END {if(count>0) printf "%.2f", sum/count; else print "N/A"}')
    phase_max=$(grep "^phase${phase}-" /tmp/scenario-04-results.log 2>/dev/null | awk -F'in ' '{print $2}' | awk -F's' '{if ($1 > max) max=$1} END {printf "%.2f", max}')
    phase_slow=$(grep "^phase${phase}-" /tmp/scenario-04-results.log 2>/dev/null | awk -F'in ' '{print $2}' | awk -F's' '{if ($1 > 5) count++} END {print count+0}')

    local phase_success=$((phase_total - phase_errors))

    echo ""
    echo "  ┌─────────────────────────────────────┐"
    echo "  │ Phase $phase 결과 ($label)"
    echo "  ├─────────────────────────────────────┤"
    echo "  │ 동시 요청:      $concurrent건"
    echo "  │ 소요 시간:      ${elapsed}초"
    printf "  │ 성공/실패:      %s/%s\n" "$phase_success" "$phase_errors"
    echo "  │ 평균 응답시간:  ${phase_avg}s"
    echo "  │ 최대 응답시간:  ${phase_max}s"
    echo "  │ 5초 초과:       ${phase_slow}건"
    echo "  └─────────────────────────────────────┘"
    echo ""
}

# --- DB/서비스 상태 스냅샷 ---
take_snapshot() {
    local label=$1
    log_info "--- 시스템 상태 ($label) ---"

    # DB 연결 상태
    kubectl -n "$NAMESPACE" exec "$PG_POD" -- \
        psql -U plopvape -d plopvape -t -A \
        -c "SELECT 'DB 연결: ' || count(*) || '/' || (SELECT setting FROM pg_settings WHERE name='max_connections') || ' (idle:' || count(*) FILTER (WHERE state='idle') || ', active:' || count(*) FILTER (WHERE state='active') || ')' FROM pg_stat_activity WHERE datname='plopvape';" 2>/dev/null || true

    # Pod 리소스
    kubectl --kubeconfig=/home/nkia/.kube/config -n "$NAMESPACE" top pod --no-headers 2>/dev/null | \
        grep -E "order|product|inventory|payment" | \
        awk '{printf "  %-45s CPU:%-6s MEM:%s\n", $1, $2, $3}' || true
}

# --- 최종 결과 분석 ---
analyze_results() {
    log_info "=== 전체 결과 분석 ==="

    if [[ ! -f /tmp/scenario-04-results.log ]]; then
        log_warn "결과 로그 없음"
        return
    fi

    echo ""
    echo "╔═══════════════════════════════════════════════════════════╗"
    echo "║            시나리오 04: 트래픽 폭주 결과 종합              ║"
    echo "╠═══════════════════════════════════════════════════════════╣"

    for phase in 1 2 3 4; do
        local count errors avg
        count=$(grep "^phase${phase}-" /tmp/scenario-04-results.log 2>/dev/null | wc -l)
        if [[ "$count" == "0" ]]; then continue; fi
        errors=$(grep "^phase${phase}-" /tmp/scenario-04-results.log 2>/dev/null | grep "HTTP [45]" | wc -l)
        avg=$(grep "^phase${phase}-" /tmp/scenario-04-results.log 2>/dev/null | awk -F'in ' '{print $2}' | awk -F's' '{sum+=$1; count++} END {if(count>0) printf "%.2f", sum/count; else print "N/A"}')
        local success=$((count - errors))
        local error_pct=0
        if [[ "$count" -gt 0 ]]; then
            error_pct=$((errors * 100 / count))
        fi

        printf "║ Phase %d: %3d건 → 성공:%3d 에러:%3d (%2d%%) 평균:%6ss ║\n" \
            "$phase" "$count" "$success" "$errors" "$error_pct" "$avg"
    done

    echo "╚═══════════════════════════════════════════════════════════╝"
    echo ""

    # 전체 통계
    local total errors
    total=$(wc -l < /tmp/scenario-04-results.log)
    errors=$(grep "HTTP [45]" /tmp/scenario-04-results.log 2>/dev/null | wc -l)
    local error_pct=0
    if [[ "$total" -gt 0 ]]; then
        error_pct=$((errors * 100 / total))
    fi
    log_info "전체: ${total}건 중 ${errors}건 에러 (${error_pct}%)"
}

# --- 원상복구 ---
cleanup() {
    log_info "=== 원상복구 시작 ==="

    # 재고 복원
    kubectl -n "$NAMESPACE" exec "$PG_POD" -- \
        psql -U plopvape -d plopvape \
        -c "UPDATE inventory_schema.inventory SET stock = 50 WHERE product_id <= 4;
            UPDATE inventory_schema.inventory SET stock = 200 WHERE product_id BETWEEN 5 AND 8;
            UPDATE inventory_schema.inventory SET stock = 300 WHERE product_id BETWEEN 9 AND 12;
            UPDATE inventory_schema.inventory SET stock = 100 WHERE product_id BETWEEN 13 AND 16;" 2>/dev/null || true
    log_ok "재고 원래 수준으로 복원"

    # Pod rolling restart: 트래픽 폭주로 확장된 JVM 힙을 초기화하여
    # post-burst baseline 잔재가 다음 시나리오로 이월되지 않게 한다.
    # (실제 운영에서도 블랙프라이데이 후 rolling restart는 표준 관행)
    log_info "서비스 rolling restart 중 (JVM baseline 초기화)..."
    local services=(testbed-order testbed-product testbed-inventory testbed-payment testbed-notification)
    for svc in "${services[@]}"; do
        kubectl -n "$NAMESPACE" rollout restart deployment "$svc" 2>/dev/null || true
    done
    for svc in "${services[@]}"; do
        kubectl -n "$NAMESPACE" rollout status deployment "$svc" --timeout=180s 2>/dev/null || \
            log_warn "$svc rollout 대기 타임아웃"
    done
    log_ok "전 서비스 rolling restart 완료"

    # API 정상 확인
    sleep 3
    local api_code
    api_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 "$API_BASE/api/products" 2>/dev/null || echo "000")
    if [[ "$api_code" == "200" ]]; then
        log_ok "API 정상 확인 (HTTP 200)"
    else
        log_warn "API 상태: HTTP $api_code"
    fi

    rm -f /tmp/scenario-04-*.log
    log_ok "=== 원상복구 완료 ==="
}

# --- 메인 실행 ---
main() {
    echo ""
    echo "╔═══════════════════════════════════════════════════════════╗"
    echo "║       시나리오 04: 블랙프라이데이 트래픽 폭주              ║"
    echo "║                                                           ║"
    echo "║  평소 5건 → 30건 → 100건 → 200건 동시 주문               ║"
    echo "║  인위적 주입 없이 순수 트래픽만으로 장애 재현              ║"
    echo "║                                                           ║"
    echo "║  시작: $(date '+%Y-%m-%d %H:%M:%S')                            ║"
    echo "╚═══════════════════════════════════════════════════════════╝"
    echo ""

    rm -f /tmp/scenario-04-results.log

    trap cleanup EXIT

    check_prerequisites
    echo ""

    # Phase 1: 정상 트래픽 (베이스라인)
    take_snapshot "Phase 1 시작 전"
    send_orders 1 "$PHASE_1_CONCURRENT" "정상 트래픽"
    sleep 3

    # Phase 2: 증가 트래픽
    take_snapshot "Phase 2 시작 전"
    send_orders 2 "$PHASE_2_CONCURRENT" "증가 트래픽 (6x)"
    sleep 3

    # Phase 3: 폭주 트래픽
    take_snapshot "Phase 3 시작 전"
    send_orders 3 "$PHASE_3_CONCURRENT" "폭주 트래픽 (20x)"
    sleep 3

    # Phase 4: 극단적 폭주
    take_snapshot "Phase 4 시작 전"
    send_orders 4 "$PHASE_4_CONCURRENT" "극단적 폭주 (40x)"

    echo ""
    take_snapshot "테스트 완료 후"
    echo ""

    # 전체 분석
    analyze_results

    echo ""
    echo "============================================================"
    echo "  시나리오 04 완료"
    echo "  종료 시간: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "============================================================"
    echo ""

    log_info "원상복구: $0 cleanup"
}

# --- 실행 분기 ---
if [[ "${1:-}" == "cleanup" ]]; then
    cleanup
    trap - EXIT
else
    main
fi
