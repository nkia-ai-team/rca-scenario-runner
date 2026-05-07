#!/usr/bin/env bash
# =============================================================================
# 시나리오 03: PostgreSQL Pod CPU Throttling → 전체 서비스 캐스케이드 슬로우다운
# =============================================================================
# Root Cause: PostgreSQL Pod의 K8s CPU limit을 극도로 낮게 변경하여(500m → 30m)
#             cgroup CPU throttling을 유발하고, 전체 서비스의 DB 쿼리 지연 →
#             응답 지연 캐스케이드를 재현한다.
#
# 전파 경로: DB CPU throttle → 전체 SQL 지연 → 4개 서비스 동시 슬로우다운 → Nginx timeout
#
# 사용법:
#   ./scenario-03-db-cpu-throttle.sh          # 시나리오 실행
#   ./scenario-03-db-cpu-throttle.sh cleanup  # 원상복구
#
# 실행 환경: K3s + food-delivery 이 배포된 호스트 (rca-scenario-runner 가 같은 호스트에 떠있음)
# =============================================================================

set -uo pipefail

# --- 설정 ---
export KUBECONFIG=/home/nkia/.kube/config
NAMESPACE="rca-testbed-food"
PG_STATEFULSET="testbed-postgres"
PG_POD="testbed-postgres-0"
API_BASE="http://127.0.0.1:30080"  # NOTE food-delivery 는 NodePort 필요. 사용자 별 NodePort 30080 설정 필요
ORIGINAL_CPU_LIMIT="500m"
ORIGINAL_CPU_REQUEST="200m"
THROTTLE_CPU_LIMIT="10m"        # 극도로 낮은 CPU limit
THROTTLE_CPU_REQUEST="10m"
LOAD_DURATION=60                 # 부하 지속 시간 (초)
CONCURRENT_REQUESTS=5            # 동시 요청 수

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

    # kubectl 접근 확인
    if ! kubectl -n "$NAMESPACE" get statefulset "$PG_STATEFULSET" &>/dev/null; then
        log_error "PostgreSQL StatefulSet ($PG_STATEFULSET) 접근 불가"
        exit 1
    fi

    # 현재 CPU 설정 확인
    local current_limit
    current_limit=$(kubectl -n "$NAMESPACE" get statefulset "$PG_STATEFULSET" \
        -o jsonpath='{.spec.template.spec.containers[0].resources.limits.cpu}' 2>/dev/null)
    log_info "현재 PostgreSQL CPU limit: $current_limit"

    # API 접근 확인
    local http_code
    http_code=$(curl -s -o /dev/null -w "%{http_code}" "$API_BASE/api/restaurants" 2>/dev/null || echo "000")
    if [[ "$http_code" != "200" ]]; then
        log_error "API 접근 불가 (HTTP $http_code)"
        exit 1
    fi

    # 재고 확인 및 보충
    kubectl -n "$NAMESPACE" exec "$PG_POD" -- \
        psql -U plopvape -d plopvape \
        -c "UPDATE orders_schema.orders SET stock = GREATEST(stock, 200);" 2>/dev/null
    log_ok "재고 보충 완료"

    log_ok "사전 조건 확인 완료"
}

# --- 베이스라인 측정 ---
measure_baseline() {
    log_info "베이스라인 응답시간 측정 중..." >&2

    # 상품 조회
    local restaurant_time
    restaurant_time=$(curl -s -o /dev/null -w "%{time_total}" "$API_BASE/api/restaurants" 2>/dev/null || echo "N/A")
    log_info "베이스라인 GET /api/restaurants: ${restaurant_time}s" >&2

    # 주문 생성
    local order_time
    order_time=$(curl -s -o /dev/null -w "%{time_total}" \
        --max-time 30 \
        -X POST "$API_BASE/api/orders" \
        -H "Content-Type: application/json" \
        -d '{"customerName":"baseline","customerEmail":"baseline@test.com","items":[{"restaurantId":5,"quantity":1}]}' 2>/dev/null || echo "N/A")
    log_info "베이스라인 POST /api/orders: ${order_time}s" >&2

    # DB 쿼리 직접 실행 시간
    local db_time
    db_time=$(kubectl -n "$NAMESPACE" exec "$PG_POD" -- \
        psql -U plopvape -d plopvape -t -A \
        -c "\\timing on" \
        -c "SELECT count(*) FROM restaurant_schema.restaurants p JOIN orders_schema.orders i ON p.id = i.restaurant_id;" 2>&1 | grep "Time:" | awk '{print $2}' || echo "N/A")
    log_info "베이스라인 DB 쿼리 시간: ${db_time:-N/A} ms" >&2

    echo "$restaurant_time|$order_time"
}

# --- CPU limit 변경 ---
apply_cpu_throttle() {
    log_info "=== PostgreSQL Pod CPU limit 변경: $ORIGINAL_CPU_LIMIT → $THROTTLE_CPU_LIMIT ==="

    # StatefulSet patch
    kubectl -n "$NAMESPACE" patch statefulset "$PG_STATEFULSET" --type='json' \
        -p="[
            {\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/resources/limits/cpu\",\"value\":\"$THROTTLE_CPU_LIMIT\"},
            {\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/resources/requests/cpu\",\"value\":\"$THROTTLE_CPU_REQUEST\"}
        ]" 2>&1

    # StatefulSet은 패치만으로 Pod 재시작이 안 됨 - 강제 재시작 필요
    log_info "Pod 강제 재시작 (StatefulSet 패치만으로는 기존 Pod에 적용 안 됨)..."
    kubectl -n "$NAMESPACE" delete pod "$PG_POD" --grace-period=5 2>&1
    sleep 3

    log_info "PostgreSQL Pod 재시작 대기 중..."

    # Pod 재시작 완료 대기
    local max_wait=120
    local waited=0
    while [[ $waited -lt $max_wait ]]; do
        local ready
        ready=$(kubectl -n "$NAMESPACE" get pod "$PG_POD" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "False")
        if [[ "$ready" == "True" ]]; then
            log_ok "PostgreSQL Pod Ready (${waited}초 소요)"
            break
        fi
        sleep 5
        waited=$((waited + 5))
        log_info "  대기 중... ${waited}/${max_wait}초 (상태: $ready)"
    done

    if [[ $waited -ge $max_wait ]]; then
        log_error "PostgreSQL Pod가 ${max_wait}초 내에 Ready 되지 않음"
        exit 1
    fi

    # 서비스들이 DB에 재연결할 시간
    log_info "서비스 DB 재연결 대기 (15초)..."
    sleep 15

    # 변경된 CPU limit 확인
    local new_limit
    new_limit=$(kubectl -n "$NAMESPACE" get statefulset "$PG_STATEFULSET" \
        -o jsonpath='{.spec.template.spec.containers[0].resources.limits.cpu}' 2>/dev/null)
    log_ok "변경된 CPU limit: $new_limit"
}

# --- 부하 생성 ---
generate_load() {
    log_info "=== 부하 생성 시작 (${LOAD_DURATION}초, 동시 ${CONCURRENT_REQUESTS}건) ==="

    local end_time=$(($(date +%s) + LOAD_DURATION))
    local request_count=0
    local round=0

    while [[ $(date +%s) -lt $end_time ]]; do
        ((round++))
        local pids=()

        # 동시 요청: 상품 조회 + 주문 생성 + 무거운 DB 쿼리 혼합
        for i in $(seq 1 "$CONCURRENT_REQUESTS"); do
            if (( i % 3 == 0 )); then
                # 상품 조회 (SELECT)
                curl -s -o /dev/null -w "GET-restaurants-${round}-${i}: HTTP %{http_code} in %{time_total}s\n" \
                    --max-time 30 \
                    "$API_BASE/api/restaurants" \
                    >> /tmp/scenario-03-results.log 2>&1 &
            elif (( i % 3 == 1 )); then
                # 주문 생성 (SELECT + INSERT + UPDATE - 전체 호출 체인)
                curl -s -o /dev/null -w "POST-order-${round}-${i}: HTTP %{http_code} in %{time_total}s\n" \
                    --max-time 60 \
                    -X POST "$API_BASE/api/orders" \
                    -H "Content-Type: application/json" \
                    -d "{\"customerName\":\"throttle-${round}-${i}\",\"customerEmail\":\"t${i}@test.com\",\"items\":[{\"restaurantId\":$((i % 16 + 1)),\"quantity\":1}]}" \
                    >> /tmp/scenario-03-results.log 2>&1 &
            else
                # 무거운 DB 쿼리 (CPU throttle 하에서 추가 부하 발생)
                kubectl -n "$NAMESPACE" exec "$PG_POD" -- \
                    psql -U plopvape -d plopvape -t -A \
                    -c "SELECT count(*) FROM generate_series(1,10000) a CROSS JOIN generate_series(1,100) b;" \
                    &>/dev/null &
            fi
            pids+=($!)
            ((request_count++)) || true
        done

        # 현재 라운드 완료 대기
        for pid in "${pids[@]}"; do
            wait "$pid" 2>/dev/null || true
        done

        local remaining=$(( end_time - $(date +%s) ))
        if (( round % 3 == 0 )) && (( remaining > 0 )); then
            log_info "  라운드 $round 완료, 총 ${request_count}건, 남은 시간: ${remaining}초"
        fi

        sleep 1
    done

    log_ok "부하 생성 완료: 총 ${request_count}건"
}

# --- DB CPU 사용 상태 확인 ---
check_db_cpu_status() {
    log_info "=== PostgreSQL Pod 리소스 상태 ==="

    # Pod 리소스 사용량
    kubectl -n "$NAMESPACE" top pod "$PG_POD" 2>/dev/null || log_warn "metrics-server 미설치로 top 불가"

    # DB 활성 세션
    log_info "=== DB 활성 세션 ==="
    kubectl -n "$NAMESPACE" exec "$PG_POD" -- \
        psql -U plopvape -d plopvape \
        -c "SELECT state, count(*), avg(EXTRACT(EPOCH FROM (now() - query_start)))::numeric(10,2) as avg_duration_sec
            FROM pg_stat_activity
            WHERE datname='plopvape'
            GROUP BY state;" 2>/dev/null

    # 쿼리 시간 직접 측정
    log_info "=== DB 쿼리 직접 시간 측정 ==="
    kubectl -n "$NAMESPACE" exec "$PG_POD" -- \
        psql -U plopvape -d plopvape \
        -c "\\timing on" \
        -c "SELECT count(*) FROM restaurant_schema.restaurants p JOIN orders_schema.orders i ON p.id = i.restaurant_id;" 2>/dev/null
}

# --- 결과 분석 ---
analyze_results() {
    log_info "=== 결과 분석 ==="

    if [[ -f /tmp/scenario-03-results.log ]]; then
        local total
        total=$(wc -l < /tmp/scenario-03-results.log)

        # GET과 POST 분리 분석
        local get_count get_avg get_max
        get_count=$(grep -c "^GET-" /tmp/scenario-03-results.log 2>/dev/null || echo "0")
        get_avg=$(grep "^GET-" /tmp/scenario-03-results.log 2>/dev/null | awk -F'in ' '{print $2}' | awk -F's' '{sum+=$1; count++} END {if(count>0) printf "%.2f", sum/count; else print "N/A"}')
        get_max=$(grep "^GET-" /tmp/scenario-03-results.log 2>/dev/null | awk -F'in ' '{print $2}' | awk -F's' '{if ($1 > max) max=$1} END {printf "%.2f", max}')

        local post_count post_avg post_max
        post_count=$(grep -c "^POST-" /tmp/scenario-03-results.log 2>/dev/null || echo "0")
        post_avg=$(grep "^POST-" /tmp/scenario-03-results.log 2>/dev/null | awk -F'in ' '{print $2}' | awk -F's' '{sum+=$1; count++} END {if(count>0) printf "%.2f", sum/count; else print "N/A"}')
        post_max=$(grep "^POST-" /tmp/scenario-03-results.log 2>/dev/null | awk -F'in ' '{print $2}' | awk -F's' '{if ($1 > max) max=$1} END {printf "%.2f", max}')

        local errors
        errors=$(grep -c "HTTP [45]" /tmp/scenario-03-results.log 2>/dev/null || echo "0")
        local slow_requests
        slow_requests=$(awk -F'in ' '{print $2}' /tmp/scenario-03-results.log 2>/dev/null | awk -F's' '{if ($1 > 5) count++} END {print count+0}')

        echo ""
        echo "======================================"
        echo "  시나리오 03 결과 요약"
        echo "======================================"
        echo "  총 요청:           $total"
        echo "  HTTP 에러:         $errors"
        echo "  5초 초과 응답:     $slow_requests"
        echo ""
        echo "  GET /api/restaurants:"
        echo "    요청 수:         $get_count"
        echo "    평균 응답시간:   ${get_avg}s"
        echo "    최대 응답시간:   ${get_max}s"
        echo ""
        echo "  POST /api/orders:"
        echo "    요청 수:         $post_count"
        echo "    평균 응답시간:   ${post_avg}s"
        echo "    최대 응답시간:   ${post_max}s"
        echo "======================================"
        echo ""
    else
        log_warn "결과 로그 파일 없음"
    fi
}

# --- 원상복구 ---
cleanup() {
    log_info "=== 원상복구 시작 ==="

    # 1. CPU limit 복원
    local current_limit
    current_limit=$(kubectl -n "$NAMESPACE" get statefulset "$PG_STATEFULSET" \
        -o jsonpath='{.spec.template.spec.containers[0].resources.limits.cpu}' 2>/dev/null)

    if [[ "$current_limit" != "$ORIGINAL_CPU_LIMIT" ]]; then
        log_info "CPU limit 복원: $current_limit → $ORIGINAL_CPU_LIMIT"
        kubectl -n "$NAMESPACE" patch statefulset "$PG_STATEFULSET" --type='json' \
            -p="[
                {\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/resources/limits/cpu\",\"value\":\"$ORIGINAL_CPU_LIMIT\"},
                {\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/resources/requests/cpu\",\"value\":\"$ORIGINAL_CPU_REQUEST\"}
            ]" 2>&1

        # StatefulSet은 패치만으로 Pod 재시작이 안 됨 - 강제 재시작 필요
        log_info "Pod 강제 재시작..."
        kubectl -n "$NAMESPACE" delete pod "$PG_POD" --grace-period=5 2>&1 || true
        sleep 3

        log_info "PostgreSQL Pod 재시작 대기 중..."
        local max_wait=120
        local waited=0
        while [[ $waited -lt $max_wait ]]; do
            local ready
            ready=$(kubectl -n "$NAMESPACE" get pod "$PG_POD" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || echo "False")
            if [[ "$ready" == "True" ]]; then
                log_ok "PostgreSQL Pod Ready (${waited}초 소요)"
                break
            fi
            sleep 5
            waited=$((waited + 5))
            log_info "  대기 중... ${waited}/${max_wait}초"
        done
    else
        log_info "CPU limit 이미 정상 ($current_limit)"
    fi

    # 2. 서비스 DB 재연결 대기
    sleep 15
    log_info "서비스 DB 재연결 대기 완료"

    # 3. 재고 보충
    kubectl -n "$NAMESPACE" exec "$PG_POD" -- \
        psql -U plopvape -d plopvape \
        -c "UPDATE orders_schema.orders SET stock = GREATEST(stock, 50);" 2>/dev/null || true
    log_ok "재고 보충 완료"

    # 4. API 정상 확인
    local api_code
    api_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 "$API_BASE/api/restaurants" 2>/dev/null || echo "000")
    if [[ "$api_code" == "200" ]]; then
        log_ok "API 정상 복구 확인 (HTTP 200)"
    else
        log_warn "API 복구 대기 중 (HTTP $api_code)"
    fi

    # 5. 임시 파일 정리
    rm -f /tmp/scenario-03-*.log

    log_ok "=== 원상복구 완료 ==="
}

# --- 메인 실행 ---
main() {
    echo ""
    echo "============================================================"
    echo "  시나리오 03: PostgreSQL Pod CPU Throttling"
    echo "  시작 시간: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "============================================================"
    echo ""

    # 이전 결과 파일 정리
    rm -f /tmp/scenario-03-results.log

    trap cleanup EXIT

    check_prerequisites
    echo ""

    # 베이스라인 측정
    local baseline
    baseline=$(measure_baseline)
    echo ""

    # CPU throttle 적용
    apply_cpu_throttle
    echo ""

    # throttle 후 DB 상태 확인
    check_db_cpu_status
    echo ""

    # 부하 생성
    generate_load
    echo ""

    # 부하 중 DB 상태 재확인
    check_db_cpu_status
    echo ""

    # 결과 분석
    analyze_results

    echo ""
    echo "============================================================"
    echo "  시나리오 03 완료"
    echo "  종료 시간: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "  베이스라인: ${baseline}"
    echo "  원상복구: $0 cleanup"
    echo "============================================================"
    echo ""

    log_warn "원상복구하려면: $0 cleanup"
    log_warn "cleanup은 PostgreSQL Pod 재시작을 포함하므로 약 1-2분 소요됩니다."
}

# --- 실행 분기 ---
if [[ "${1:-}" == "cleanup" ]]; then
    cleanup
    trap - EXIT
else
    main
fi
