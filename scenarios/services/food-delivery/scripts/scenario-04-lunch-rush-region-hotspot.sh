#!/usr/bin/env bash
# =============================================================================
# 시나리오 04: Lunch Rush Region Hot-Spot (지역 집중 트래픽 + 데이터 분포 편향)
# =============================================================================
# 도메인 배경: 점심 시간 (11:30~13:00) 강남 오피스 밀집 지역 단일 region (GANGNAM)
#              에 트래픽 집중. food-delivery 의 데이터 분포 특성: restaurant.region
#              컬럼 + index 가 있지만 region='GANGNAM' 만 hit 시 한 region 의
#              restaurants/menus row 만 hot, 다른 region 데이터는 cold → DB
#              cache miss + region-specific 식당의 orders 테이블 partition skew.
#
# Root Cause: GANGNAM region 식당 (restaurant_id=1) 만 집중 타격하는 단일 region
#             폭주 — 일반적인 traffic flood (전 region 균등) 와 달리 데이터 분포
#             편향. 같은 restaurant row + 같은 menus 들에 동시 SELECT/UPDATE 폭증.
#
# 전파 경로: region=GANGNAM POST /api/orders 폭주 → restaurant_id=1 row hot →
#            menus(restaurant_id=1) 5개 row 만 hit → orders 의 restaurant_id=1
#            인덱스만 사용 → DB cache LRU 가 한 region 만 hot, 다른 region 콜드
#
# plopvape 04 (균등 traffic flood) 와의 차이: 본 시나리오는 **데이터 분포 편향**
#   을 측정 — region 인덱스 효과 + cache skew + restaurant-service 의 단일 row
#   조회 hot path. 균등 분산 flood 가 잡지 못하는 특정 row hotspot 알람 검증.
#
# 사용법:
#   ./scenario-04-lunch-rush-region-hotspot.sh         # 실행
#   ./scenario-04-lunch-rush-region-hotspot.sh cleanup # 원상복구
# =============================================================================

set -uo pipefail

export KUBECONFIG=/home/nkia/.kube/food-delivery.yaml
NAMESPACE="rca-testbed-food"
DB_POD="testbed-postgres-0"
DB_USER="fooddelivery"
DB_PASS="fooddelivery1234"
DB_NAME="fooddelivery"
ORDER_POD_LABEL="app=testbed-order"
HOT_RESTAURANT_ID=1         # GANGNAM 강남 떡볶이 본점
HOT_MENU_RANGE_START=1      # 메뉴 1~5 (restaurant_id=1 의 메뉴)
HOT_MENU_RANGE_END=5
PHASE_DURATION=60
PHASES=("10" "60" "150" "400")

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log_info()  { echo -e "${CYAN}[INFO]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC}   $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }

psql_exec() {
    kubectl -n "$NAMESPACE" exec "$DB_POD" -- env PGPASSWORD="$DB_PASS" \
        psql -U "$DB_USER" -d "$DB_NAME" "$@"
}

order_pod() {
    kubectl -n "$NAMESPACE" get pod -l "$ORDER_POD_LABEL" -o jsonpath='{.items[0].metadata.name}'
}

check_prerequisites() {
    log_info "사전 조건 확인 중..."
    local rinfo
    rinfo=$(psql_exec -t -A -c "SELECT id, name, region, status FROM restaurants WHERE id=$HOT_RESTAURANT_ID;" 2>/dev/null)
    if [[ -z "$rinfo" ]]; then
        log_error "Hot restaurant id=$HOT_RESTAURANT_ID 없음"; exit 1
    fi
    log_info "Hot restaurant: $rinfo"
    psql_exec -c "UPDATE restaurants SET status='OPEN' WHERE id=$HOT_RESTAURANT_ID;" 2>/dev/null
    psql_exec -c "UPDATE menus SET available=TRUE WHERE restaurant_id=$HOT_RESTAURANT_ID;" 2>/dev/null
    local op; op=$(order_pod)
    [[ -z "$op" ]] && { log_error "order pod 없음"; exit 1; }
    log_ok "사전 조건 OK"
}

flood_phase_hotspot() {
    local concurrency=$1
    local phase_num=$2
    log_info "=== Phase $phase_num: GANGNAM 단일 region 동시 $concurrency 요청 (${PHASE_DURATION}s) ==="
    local op; op=$(order_pod)
    local end_time; end_time=$(($(date +%s) + PHASE_DURATION))
    local issued=0

    while [ "$(date +%s)" -lt "$end_time" ]; do
        local pids=()
        for i in $(seq 1 "$concurrency"); do
            # 메뉴는 hot restaurant 의 1~5 중 random — restaurant_id=1 의 5개 menu row 만 hit
            local mid=$((HOT_MENU_RANGE_START + (RANDOM % (HOT_MENU_RANGE_END - HOT_MENU_RANGE_START + 1))))
            kubectl -n "$NAMESPACE" exec "$op" -- \
                curl -s -o /dev/null \
                -w "lr-p${phase_num}-${i}: HTTP %{http_code} in %{time_total}s\n" \
                --max-time 20 \
                -X POST "http://localhost:8080/api/orders" \
                -H "Content-Type: application/json" \
                -d "{\"customerId\":\"lunch-${phase_num}-${i}\",\"restaurantId\":$HOT_RESTAURANT_ID,\"items\":[{\"menuId\":$mid,\"qty\":1}]}" \
                >> /tmp/scenario-04-fd-results.log 2>&1 &
            pids+=($!)
            issued=$((issued + 1))
        done
        for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
    done
    log_info "Phase $phase_num 완료: $issued 요청 (전부 restaurant_id=$HOT_RESTAURANT_ID hit)"
}

monitor_region_skew() {
    log_info "=== 데이터 분포 검증 ==="
    psql_exec -c "
        SELECT r.region,
               r.name,
               (SELECT count(*) FROM orders o WHERE o.restaurant_id = r.id) as order_count
        FROM restaurants r
        ORDER BY r.id;
    " 2>/dev/null || true
}

analyze_results() {
    log_info "=== 결과 분석 ==="
    if [[ -f /tmp/scenario-04-fd-results.log ]]; then
        local total errors avg_time max_time
        total=$(wc -l < /tmp/scenario-04-fd-results.log)
        errors=$(grep -c "HTTP [45]" /tmp/scenario-04-fd-results.log 2>/dev/null || echo "0")
        avg_time=$(awk -F'in ' '{print $2}' /tmp/scenario-04-fd-results.log 2>/dev/null | awk -F's' '{s+=$1; c++} END {if(c>0) printf "%.2f", s/c; else print "N/A"}')
        max_time=$(awk -F'in ' '{print $2}' /tmp/scenario-04-fd-results.log 2>/dev/null | awk -F's' '{if ($1>m) m=$1} END {printf "%.2f", m}')
        echo "======================================"
        echo "  시나리오 04: Lunch Rush GANGNAM Hot-Spot"
        echo "======================================"
        echo "  총 요청:        $total"
        echo "  HTTP 에러:      $errors"
        echo "  평균 응답시간:  ${avg_time}s (예상: 한 row hot 으로 증가)"
        echo "  최대 응답시간:  ${max_time}s"
        echo "======================================"
        log_info "기대: restaurant_id=1 orders 폭증 + 다른 region orders 정체 → DPM partition skew"
    fi
}

cleanup() {
    log_info "=== 원상복구 ==="
    log_info "4 service rolling restart (thread pool reset)"
    for svc in testbed-order testbed-restaurant testbed-dispatch testbed-payment; do
        kubectl -n "$NAMESPACE" rollout restart deployment/"$svc" 2>/dev/null || true
    done
    for svc in testbed-order testbed-restaurant testbed-dispatch testbed-payment; do
        kubectl -n "$NAMESPACE" rollout status deployment/"$svc" --timeout=120s 2>/dev/null || true
    done
    # 시나리오 도중 들어온 real 주문의 ASSIGNED dispatch 정리 — capacity 누적 방지.
    psql_exec -c "UPDATE dispatches SET status='DELIVERED' WHERE status='ASSIGNED';" 2>/dev/null || true
    rm -f /tmp/scenario-04-fd-*.log
    log_ok "rolling restart 완료 + leftover ASSIGNED bulk DELIVERED"
}

main() {
    echo "============================================================"
    echo "  시나리오 04: Lunch Rush Region Hot-Spot (data skew)"
    echo "  시작: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "============================================================"
    rm -f /tmp/scenario-04-fd-results.log
    trap cleanup EXIT
    check_prerequisites; echo
    local phase_num=1
    for c in "${PHASES[@]}"; do
        flood_phase_hotspot "$c" "$phase_num"
        monitor_region_skew
        echo
        phase_num=$((phase_num + 1))
    done
    analyze_results
    echo "  시나리오 04 완료"
}

if [[ "${1:-}" == "cleanup" ]]; then cleanup; trap - EXIT; else main; fi
