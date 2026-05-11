#!/usr/bin/env bash
# =============================================================================
# 시나리오 02: 외부 PG API 무응답 → 결제/주문 캐스케이드 장애
# =============================================================================
# Root Cause: pg-mock을 중단하고 TCP black-hole 리스너로 대체하여 외부 PG API를
#             무응답 상태로 만든다. payment-service의 RestClient read timeout(10s)
#             동안 스레드가 블로킹되어 order-service까지 캐스케이드 장애가 발생한다.
#
# 전파 경로: pg-mock 무응답 → payment read timeout(10s) → order thread starvation → 502
#
# 사용법:
#   ./scenario-02-payment-timeout.sh          # 시나리오 실행
#   ./scenario-02-payment-timeout.sh cleanup  # 원상복구
#
# 실행 환경: K3s + plopvape-shop 이 배포된 호스트 (rca-scenario-runner 가 같은 호스트에 떠있음)
# =============================================================================

set -uo pipefail

# --- 설정 ---
export KUBECONFIG=/home/nkia/.kube/config
NAMESPACE="${NAMESPACE:-rca-testbed-plopvape}"
API_BASE="${API_BASE:-http://127.0.0.1:30080}"
PG_MOCK_CONTAINER="pg-mock"
PG_MOCK_PORT=8090
BLACKHOLE_PID_FILE="/tmp/scenario-02-blackhole.pid"
CONCURRENT_ORDERS=15        # 동시 주문 요청 수 (스레드 풀 포화 목적)
ORDER_ROUNDS=2              # 반복 횟수
# payment-service RestClient: connect-timeout=3s, read-timeout=10s

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

    # pg-mock 컨테이너 확인
    local pg_status
    pg_status=$(docker inspect -f '{{.State.Status}}' "$PG_MOCK_CONTAINER" 2>/dev/null || echo "not_found")
    if [[ "$pg_status" == "not_found" ]]; then
        log_error "pg-mock 컨테이너를 찾을 수 없습니다"
        exit 1
    fi
    if [[ "$pg_status" == "paused" ]]; then
        docker unpause "$PG_MOCK_CONTAINER" 2>/dev/null || true
        sleep 2
    fi
    log_ok "pg-mock 컨테이너 상태: $pg_status"

    # python3 확인
    if ! command -v python3 &>/dev/null; then
        log_error "python3이 필요합니다 (black-hole 리스너용)"
        exit 1
    fi

    # API 접근 확인
    local http_code
    http_code=$(curl -s -o /dev/null -w "%{http_code}" "$API_BASE/api/products" 2>/dev/null || echo "000")
    if [[ "$http_code" != "200" ]]; then
        log_error "API 접근 불가 (HTTP $http_code)"
        exit 1
    fi

    # 재고 확인 및 보충
    kubectl -n "$NAMESPACE" exec testbed-postgres-0 -- \
        psql -U plopvape -d plopvape \
        -c "UPDATE inventory_schema.inventory SET stock = GREATEST(stock, 200);" 2>/dev/null
    log_ok "재고 보충 완료 (최소 200)"

    log_ok "사전 조건 확인 완료"
}

# --- 베이스라인 측정 ---
measure_baseline() {
    log_info "베이스라인 응답시간 측정 중..." >&2

    local order_time
    order_time=$(curl -s -o /dev/null -w "%{time_total}" \
        --max-time 30 \
        -X POST "$API_BASE/api/orders" \
        -H "Content-Type: application/json" \
        -d '{"customerName":"baseline","customerEmail":"baseline@test.com","items":[{"productId":5,"quantity":1}]}' 2>/dev/null || echo "N/A")
    log_info "베이스라인 주문 응답시간: ${order_time}s" >&2

    local product_time
    product_time=$(curl -s -o /dev/null -w "%{time_total}" \
        "$API_BASE/api/products" 2>/dev/null || echo "N/A")
    log_info "베이스라인 상품조회 응답시간: ${product_time}s" >&2

    echo "$order_time"
}

# --- TCP Black-hole 리스너 시작 ---
# TCP 연결은 accept하지만 HTTP 응답을 절대 보내지 않는 서버.
# payment-service가 read-timeout(10s)까지 대기하게 만든다.
start_blackhole() {
    log_info "=== pg-mock 중단 + TCP black-hole 시작 ==="

    # 1. pg-mock 컨테이너 중단
    docker stop "$PG_MOCK_CONTAINER" &>/dev/null
    sleep 1
    log_ok "pg-mock 컨테이너 중단됨"

    # 2. 포트가 해제될 때까지 대기
    local max_wait=10
    local waited=0
    while ss -tlnp | grep -q ":${PG_MOCK_PORT} " 2>/dev/null; do
        sleep 1
        waited=$((waited + 1))
        if [[ $waited -ge $max_wait ]]; then
            log_error "포트 ${PG_MOCK_PORT}이 ${max_wait}초 내에 해제되지 않음"
            docker start "$PG_MOCK_CONTAINER" &>/dev/null
            exit 1
        fi
    done

    # 3. Python black-hole TCP 리스너 시작
    python3 -c "
import socket, threading, time
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(('0.0.0.0', ${PG_MOCK_PORT}))
s.listen(128)
print('Black-hole listening on port ${PG_MOCK_PORT}', flush=True)
def handle(conn, addr):
    # Accept connection but never send anything - let it hang
    try:
        while True:
            time.sleep(60)
    except:
        pass
    finally:
        conn.close()
while True:
    conn, addr = s.accept()
    t = threading.Thread(target=handle, args=(conn, addr), daemon=True)
    t.start()
" &>/tmp/scenario-02-blackhole.log &

    local bh_pid=$!
    echo "$bh_pid" > "$BLACKHOLE_PID_FILE"
    sleep 1

    # 4. black-hole 리스너 동작 확인
    if kill -0 "$bh_pid" 2>/dev/null; then
        log_ok "TCP black-hole 리스너 시작 (PID: $bh_pid, port: $PG_MOCK_PORT)"
    else
        log_error "Black-hole 리스너 시작 실패"
        cat /tmp/scenario-02-blackhole.log
        docker start "$PG_MOCK_CONTAINER" &>/dev/null
        exit 1
    fi

    # 5. black-hole 동작 테스트 (연결은 되지만 응답 없음 확인)
    local test_code
    test_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "http://127.0.0.1:${PG_MOCK_PORT}/health" 2>/dev/null || echo "timeout")
    log_info "Black-hole 연결 테스트: HTTP $test_code (000 또는 timeout = 정상 - 응답 없으므로)"

    log_ok "payment-service가 pg-mock:$PG_MOCK_PORT에 요청 시 TCP 연결 성공 → read timeout(10s) 대기 예상"
}

# --- 동시 주문 발생 ---
send_concurrent_orders() {
    local round=$1
    log_info "=== 동시 주문 발생 (라운드 $round: ${CONCURRENT_ORDERS}건) ==="
    log_info "각 요청이 payment read-timeout(10s)까지 대기 예상..."

    local pids=()
    local start_time
    start_time=$(date +%s)

    for i in $(seq 1 "$CONCURRENT_ORDERS"); do
        curl -s -o /tmp/scenario-02-order-${round}-${i}.log \
            -w "order-${round}-${i}: HTTP %{http_code} in %{time_total}s\n" \
            --max-time 60 \
            -X POST "$API_BASE/api/orders" \
            -H "Content-Type: application/json" \
            -d "{\"customerName\":\"timeout-${round}-${i}\",\"customerEmail\":\"timeout${i}@test.com\",\"items\":[{\"productId\":$((i % 16 + 1)),\"quantity\":1}]}" \
            >> /tmp/scenario-02-results.log 2>&1 &
        pids+=($!)
    done

    log_info "주문 ${#pids[@]}건 전송 완료, 응답 대기 중..."

    # 대기 중 상품 조회로 비-payment 경로 확인 (5초 후)
    sleep 5
    log_info "--- 장애 중 상품 조회 (payment 미사용 경로) ---"
    local product_code product_time
    product_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$API_BASE/api/products" 2>/dev/null || echo "000")
    product_time=$(curl -s -o /dev/null -w "%{time_total}" --max-time 10 "$API_BASE/api/products" 2>/dev/null || echo "timeout")
    log_info "GET /api/products: HTTP $product_code in ${product_time}s (이 경로는 정상이어야 함)"

    # 모든 주문 요청 완료 대기
    local completed=0
    for pid in "${pids[@]}"; do
        wait "$pid" 2>/dev/null || true
        ((completed++)) || true
        if (( completed % 5 == 0 )); then
            log_info "  $completed/${#pids[@]} 요청 완료..."
        fi
    done

    local end_time
    end_time=$(date +%s)
    local elapsed=$(( end_time - start_time ))

    log_ok "라운드 $round 완료 (총 ${elapsed}초 소요)"
}

# --- 서비스 상태 확인 ---
check_service_status() {
    log_info "=== 서비스 상태 확인 ==="

    for svc in "order:8080" "product:8081" "inventory:8082" "payment:8083"; do
        local name=${svc%%:*}
        local port=${svc##*:}
        local pod
        pod=$(kubectl -n "$NAMESPACE" get pods -l "app=testbed-${name}" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "N/A")
        local health
        health=$(kubectl -n "$NAMESPACE" exec "$pod" -- curl -s -o /dev/null -w "%{http_code}" --max-time 5 "http://localhost:${port}/actuator/health" 2>/dev/null || echo "N/A")
        log_info "  ${name}-service: health=$health (pod=$pod)"
    done

    log_info "=== DB 활성 세션 ==="
    kubectl -n "$NAMESPACE" exec testbed-postgres-0 -- \
        psql -U plopvape -d plopvape \
        -c "SELECT state, count(*) FROM pg_stat_activity WHERE datname='plopvape' GROUP BY state;" 2>/dev/null || true
}

# --- 결과 분석 ---
analyze_results() {
    log_info "=== 결과 분석 ==="

    if [[ -f /tmp/scenario-02-results.log ]]; then
        local total
        total=$(wc -l < /tmp/scenario-02-results.log)
        local errors
        errors=$(grep -c "HTTP [45]" /tmp/scenario-02-results.log 2>/dev/null || echo "0")
        local timeouts
        timeouts=$(awk -F'in ' '{print $2}' /tmp/scenario-02-results.log 2>/dev/null | awk -F's' '{if ($1 > 8) count++} END {print count+0}')

        local avg_time
        avg_time=$(awk -F'in ' '{print $2}' /tmp/scenario-02-results.log 2>/dev/null | awk -F's' '{sum+=$1; count++} END {if(count>0) printf "%.2f", sum/count; else print "N/A"}')

        local max_time
        max_time=$(awk -F'in ' '{print $2}' /tmp/scenario-02-results.log 2>/dev/null | awk -F's' '{if ($1 > max) max=$1} END {printf "%.2f", max}')

        echo ""
        echo "======================================"
        echo "  시나리오 02 결과 요약"
        echo "======================================"
        echo "  총 요청:          $total"
        echo "  HTTP 에러:        $errors"
        echo "  8초 초과 응답:    $timeouts"
        echo "  평균 응답시간:    ${avg_time}s"
        echo "  최대 응답시간:    ${max_time}s"
        echo "======================================"
        echo ""

        log_info "상세 응답 로그:"
        cat /tmp/scenario-02-results.log
    else
        log_warn "결과 로그 파일 없음"
    fi
}

# --- 원상복구 ---
cleanup() {
    log_info "=== 원상복구 시작 ==="

    # 1. Black-hole 프로세스 종료
    if [[ -f "$BLACKHOLE_PID_FILE" ]]; then
        local bh_pid
        bh_pid=$(cat "$BLACKHOLE_PID_FILE")
        if kill -0 "$bh_pid" 2>/dev/null; then
            kill "$bh_pid" 2>/dev/null || true
            sleep 1
            kill -9 "$bh_pid" 2>/dev/null || true
            log_ok "Black-hole 프로세스 종료 (PID: $bh_pid)"
        fi
        rm -f "$BLACKHOLE_PID_FILE"
    fi

    # 추가: 포트를 점유하고 있는 다른 프로세스도 정리
    local port_pid
    port_pid=$(ss -tlnp 2>/dev/null | grep ":${PG_MOCK_PORT} " | grep -oP 'pid=\K[0-9]+' || true)
    if [[ -n "$port_pid" ]]; then
        kill "$port_pid" 2>/dev/null || true
        sleep 1
    fi

    # 2. pg-mock 컨테이너 재시작
    local status
    status=$(docker inspect -f '{{.State.Status}}' "$PG_MOCK_CONTAINER" 2>/dev/null || echo "not_found")
    if [[ "$status" != "running" ]]; then
        docker start "$PG_MOCK_CONTAINER" &>/dev/null || true
        log_info "pg-mock 컨테이너 시작 중..."
        sleep 3
    fi
    if [[ "$status" == "paused" ]]; then
        docker unpause "$PG_MOCK_CONTAINER" &>/dev/null || true
    fi

    # 3. pg-mock 정상 동작 확인
    local max_wait=15
    local waited=0
    while [[ $waited -lt $max_wait ]]; do
        local pg_code
        pg_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "http://127.0.0.1:${PG_MOCK_PORT}/health" 2>/dev/null || echo "000")
        if [[ "$pg_code" == "200" ]]; then
            log_ok "pg-mock 정상 복구 (HTTP 200)"
            break
        fi
        sleep 2
        waited=$((waited + 2))
        log_info "  pg-mock 복구 대기 중... ${waited}/${max_wait}초 (HTTP $pg_code)"
    done

    # 4. 재고 보충
    kubectl -n "$NAMESPACE" exec testbed-postgres-0 -- \
        psql -U plopvape -d plopvape \
        -c "UPDATE inventory_schema.inventory SET stock = GREATEST(stock, 50);" 2>/dev/null || true
    log_ok "재고 보충 완료"

    # 5. 주문 API 정상 확인
    sleep 3
    local order_code
    order_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 30 \
        -X POST "$API_BASE/api/orders" \
        -H "Content-Type: application/json" \
        -d '{"customerName":"recovery-test","customerEmail":"recover@test.com","items":[{"productId":5,"quantity":1}]}' 2>/dev/null || echo "000")
    if [[ "$order_code" == "200" ]]; then
        log_ok "주문 API 정상 복구 확인 (HTTP 200)"
    else
        log_warn "주문 API 복구 대기 중 (HTTP $order_code). 스레드 풀 회복 시간 필요."
    fi

    # 6. 임시 파일 정리
    rm -f /tmp/scenario-02-*.log

    log_ok "=== 원상복구 완료 ==="
}

# --- 메인 실행 ---
main() {
    echo ""
    echo "============================================================"
    echo "  시나리오 02: 외부 PG API 무응답 → 결제/주문 캐스케이드 장애"
    echo "  시작 시간: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "  payment-service read-timeout: 10s"
    echo "============================================================"
    echo ""

    rm -f /tmp/scenario-02-results.log

    trap cleanup EXIT

    check_prerequisites
    echo ""

    local baseline
    baseline=$(measure_baseline)
    echo ""

    # TCP black-hole 시작
    start_blackhole
    echo ""

    # 동시 주문 발생
    for round in $(seq 1 "$ORDER_ROUNDS"); do
        send_concurrent_orders "$round"
        echo ""

        check_service_status
        echo ""
    done

    # 결과 분석
    analyze_results

    echo ""
    echo "============================================================"
    echo "  시나리오 02 완료"
    echo "  종료 시간: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "  베이스라인: ${baseline}s"
    echo "============================================================"
    echo ""

    log_warn "원상복구하려면: $0 cleanup"
}

# --- 실행 분기 ---
if [[ "${1:-}" == "cleanup" ]]; then
    cleanup
    trap - EXIT
else
    main
fi
