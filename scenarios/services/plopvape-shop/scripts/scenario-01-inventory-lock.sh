#!/usr/bin/env bash
# =============================================================================
# 시나리오 01: Inventory Row Lock Contention
# =============================================================================
# Root Cause: inventory 테이블에 장시간 SELECT FOR UPDATE lock을 점유한 상태에서
#             동시 주문을 발생시켜 row lock contention을 재현한다.
#
# 전파 경로: inventory lock wait → product timeout → order 5xx → Nginx 502
#
# 사용법:
#   ./scenario-01-inventory-lock.sh          # 시나리오 실행
#   ./scenario-01-inventory-lock.sh cleanup  # 원상복구
#
# 실행 환경: K3s + plopvape-shop 이 배포된 호스트 (rca-scenario-runner 가 같은 호스트에 떠있음)
# =============================================================================

set -uo pipefail

# --- 설정 ---
export KUBECONFIG=/home/nkia/.kube/config
NAMESPACE="rca-testbed"
PG_POD="testbed-postgres-0"
API_BASE="http://127.0.0.1:30080"
LOCK_PRODUCT_ID=1          # lock을 걸 상품 ID
LOCK_DURATION=120           # lock 유지 시간 (초)
CONCURRENT_ORDERS=20        # 동시 주문 요청 수
ORDER_ROUNDS=3              # 동시 주문 반복 횟수
LOCK_PID_FILE="/tmp/scenario-01-lock.pid"

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
    if ! kubectl -n "$NAMESPACE" get pod "$PG_POD" &>/dev/null; then
        log_error "PostgreSQL Pod ($PG_POD) 접근 불가"
        exit 1
    fi

    # API 접근 확인
    local http_code
    http_code=$(curl -s -o /dev/null -w "%{http_code}" "$API_BASE/api/products" 2>/dev/null || echo "000")
    if [[ "$http_code" != "200" ]]; then
        log_error "API 접근 불가 (HTTP $http_code)"
        exit 1
    fi

    # 재고 확인
    local stock
    stock=$(kubectl -n "$NAMESPACE" exec "$PG_POD" -- \
        psql -U plopvape -d plopvape -t -A \
        -c "SELECT stock FROM inventory_schema.inventory WHERE product_id = $LOCK_PRODUCT_ID;" 2>/dev/null)
    log_info "상품 $LOCK_PRODUCT_ID 현재 재고: $stock"

    if [[ "$stock" -lt 5 ]]; then
        log_warn "재고가 부족합니다. 재고를 100으로 리셋합니다."
        kubectl -n "$NAMESPACE" exec "$PG_POD" -- \
            psql -U plopvape -d plopvape \
            -c "UPDATE inventory_schema.inventory SET stock = 100 WHERE product_id = $LOCK_PRODUCT_ID;" 2>/dev/null
    fi

    log_ok "사전 조건 확인 완료"
}

# --- 베이스라인 측정 ---
measure_baseline() {
    log_info "베이스라인 응답시간 측정 중..." >&2

    local total_time
    total_time=$(curl -s -o /dev/null -w "%{time_total}" \
        --max-time 30 \
        -X POST "$API_BASE/api/orders" \
        -H "Content-Type: application/json" \
        -d "{\"customerName\":\"baseline\",\"customerEmail\":\"baseline@test.com\",\"items\":[{\"productId\":$LOCK_PRODUCT_ID,\"quantity\":1}]}" 2>/dev/null || echo "N/A")

    log_info "베이스라인 주문 응답시간: ${total_time}s" >&2
    echo "$total_time"
}

# --- Lock 트랜잭션 시작 ---
start_lock_transaction() {
    log_info "=== LOCK 트랜잭션 시작 (product_id=$LOCK_PRODUCT_ID, ${LOCK_DURATION}초) ==="

    # 백그라운드에서 장시간 lock을 잡는 트랜잭션 실행
    kubectl -n "$NAMESPACE" exec "$PG_POD" -- \
        psql -U plopvape -d plopvape \
        -c "BEGIN;
            SELECT id, product_id, stock FROM inventory_schema.inventory
            WHERE product_id = $LOCK_PRODUCT_ID FOR UPDATE;
            SELECT pg_sleep($LOCK_DURATION);
            ROLLBACK;" &>/tmp/scenario-01-lock.log &

    local lock_pid=$!
    echo "$lock_pid" > "$LOCK_PID_FILE"

    # lock이 실제로 잡혔는지 확인 (2초 대기 후)
    sleep 2

    local lock_count
    lock_count=$(kubectl -n "$NAMESPACE" exec "$PG_POD" -- \
        psql -U plopvape -d plopvape -t -A \
        -c "SELECT count(*) FROM pg_locks WHERE relation = 'inventory_schema.inventory'::regclass;" 2>/dev/null || echo "0")

    if [[ "$lock_count" -gt 0 ]]; then
        log_ok "Row lock 확인됨 (locks: $lock_count)"
        kubectl -n "$NAMESPACE" exec "$PG_POD" -- \
            psql -U plopvape -d plopvape \
            -c "SELECT locktype, mode, granted, pid FROM pg_locks WHERE relation = 'inventory_schema.inventory'::regclass;" 2>/dev/null || true
    else
        log_warn "Lock 아직 미확인 (pg_sleep 시작 대기 중일 수 있음)"
    fi

    log_ok "Lock 트랜잭션 PID: $lock_pid (${LOCK_DURATION}초 후 자동 종료)"
}

# --- 동시 주문 발생 ---
send_concurrent_orders() {
    local round=$1
    log_info "=== 동시 주문 발생 (라운드 $round: ${CONCURRENT_ORDERS}건) ==="

    local pids=()
    local start_time
    start_time=$(date +%s)

    for i in $(seq 1 "$CONCURRENT_ORDERS"); do
        curl -s -o /tmp/scenario-01-order-${round}-${i}.log \
            -w "order-${round}-${i}: HTTP %{http_code} in %{time_total}s\n" \
            -X POST "$API_BASE/api/orders" \
            -H "Content-Type: application/json" \
            -d "{\"customerName\":\"locktest-${round}-${i}\",\"customerEmail\":\"lock${i}@test.com\",\"items\":[{\"productId\":$LOCK_PRODUCT_ID,\"quantity\":1}]}" \
            >> /tmp/scenario-01-results.log 2>&1 &
        pids+=($!)
    done

    # 모든 요청 완료 대기
    local failed=0
    local succeeded=0
    for pid in "${pids[@]}"; do
        wait "$pid" 2>/dev/null && ((succeeded++)) || ((failed++)) || true
    done

    local end_time
    end_time=$(date +%s)
    local elapsed=$(( end_time - start_time ))

    log_info "라운드 $round 결과: 성공=$succeeded, 실패=$failed, 소요=${elapsed}초"
}

# --- DB Lock 상태 확인 ---
check_lock_status() {
    log_info "=== DB Lock 상태 확인 ==="

    kubectl -n "$NAMESPACE" exec "$PG_POD" -- \
        psql -U plopvape -d plopvape \
        -c "SELECT pid, state, wait_event_type, wait_event, query_start, LEFT(query, 80) as query
            FROM pg_stat_activity
            WHERE datname = 'plopvape' AND state != 'idle'
            ORDER BY query_start;" 2>/dev/null || true

    log_info "=== Lock 대기 상태 ==="
    kubectl -n "$NAMESPACE" exec "$PG_POD" -- \
        psql -U plopvape -d plopvape \
        -c "SELECT blocked.pid AS blocked_pid,
                   blocking.pid AS blocking_pid,
                   LEFT(blocked.query, 60) AS blocked_query
            FROM pg_stat_activity blocked
            JOIN pg_locks bl ON bl.pid = blocked.pid
            JOIN pg_locks lk ON lk.relation = bl.relation AND lk.pid != bl.pid
            JOIN pg_stat_activity blocking ON blocking.pid = lk.pid
            WHERE NOT bl.granted
            LIMIT 10;" 2>/dev/null || true
}

# --- 결과 분석 ---
analyze_results() {
    log_info "=== 결과 분석 ==="

    if [[ -f /tmp/scenario-01-results.log ]]; then
        local total
        total=$(wc -l < /tmp/scenario-01-results.log)
        local errors
        errors=$(grep -c "HTTP [45]" /tmp/scenario-01-results.log 2>/dev/null || echo "0")
        local timeouts
        timeouts=$(awk -F'in ' '{print $2}' /tmp/scenario-01-results.log 2>/dev/null | awk -F's' '{if ($1 > 10) count++} END {print count+0}')

        local avg_time
        avg_time=$(awk -F'in ' '{print $2}' /tmp/scenario-01-results.log 2>/dev/null | awk -F's' '{sum+=$1; count++} END {if(count>0) printf "%.2f", sum/count; else print "N/A"}')

        local max_time
        max_time=$(awk -F'in ' '{print $2}' /tmp/scenario-01-results.log 2>/dev/null | awk -F's' '{if ($1 > max) max=$1} END {printf "%.2f", max}')

        echo ""
        echo "======================================"
        echo "  시나리오 01 결과 요약"
        echo "======================================"
        echo "  총 요청:        $total"
        echo "  HTTP 에러:      $errors"
        echo "  10초 초과:      $timeouts"
        echo "  평균 응답시간:  ${avg_time}s"
        echo "  최대 응답시간:  ${max_time}s"
        echo "======================================"
        echo ""

        log_info "상세 응답 로그:"
        cat /tmp/scenario-01-results.log
    else
        log_warn "결과 로그 파일 없음"
    fi
}

# --- 원상복구 ---
cleanup() {
    log_info "=== 원상복구 시작 ==="

    # 1. lock 트랜잭션 종료
    if [[ -f "$LOCK_PID_FILE" ]]; then
        local lock_pid
        lock_pid=$(cat "$LOCK_PID_FILE")
        if kill -0 "$lock_pid" 2>/dev/null; then
            kill "$lock_pid" 2>/dev/null || true
            log_ok "Lock 트랜잭션 프로세스 종료 (PID: $lock_pid)"
        fi
        rm -f "$LOCK_PID_FILE"
    fi

    # 2. PostgreSQL에서 남은 lock 세션 강제 종료
    kubectl -n "$NAMESPACE" exec "$PG_POD" -- \
        psql -U plopvape -d plopvape \
        -c "SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = 'plopvape'
              AND state = 'active'
              AND query LIKE '%pg_sleep%'
              AND pid != pg_backend_pid();" 2>/dev/null || true

    # 3. 재고 복원
    kubectl -n "$NAMESPACE" exec "$PG_POD" -- \
        psql -U plopvape -d plopvape \
        -c "UPDATE inventory_schema.inventory SET stock = 50 WHERE product_id = $LOCK_PRODUCT_ID;" 2>/dev/null
    log_ok "상품 $LOCK_PRODUCT_ID 재고를 50으로 복원"

    # 4. 임시 파일 정리
    rm -f /tmp/scenario-01-*.log

    log_ok "=== 원상복구 완료 ==="
}

# --- 메인 실행 ---
main() {
    echo ""
    echo "============================================================"
    echo "  시나리오 01: Inventory Row Lock Contention"
    echo "  시작 시간: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "============================================================"
    echo ""

    # 이전 결과 파일 정리
    rm -f /tmp/scenario-01-results.log

    # trap으로 중단 시 cleanup 보장
    trap cleanup EXIT

    check_prerequisites
    echo ""

    # 베이스라인 측정
    local baseline
    baseline=$(measure_baseline)
    echo ""

    # Lock 시작
    start_lock_transaction
    echo ""

    # 동시 주문 발생 (여러 라운드)
    for round in $(seq 1 "$ORDER_ROUNDS"); do
        send_concurrent_orders "$round"
        echo ""

        # 라운드 사이 lock 상태 확인
        check_lock_status
        echo ""

        sleep 3
    done

    # 결과 분석
    analyze_results

    # Lock 만료 대기하지 않고 즉시 종료 (cleanup이 처리)
    echo ""
    echo "============================================================"
    echo "  시나리오 01 완료"
    echo "  종료 시간: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "  베이스라인: ${baseline}s"
    echo "============================================================"

    # trap EXIT가 cleanup을 호출함
}

# --- 실행 분기 ---
if [[ "${1:-}" == "cleanup" ]]; then
    cleanup
    trap - EXIT  # cleanup 모드에서는 EXIT trap 해제
else
    main
fi
