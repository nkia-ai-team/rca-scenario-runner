#!/usr/bin/env bash
# =============================================================================
# EX-2 / plopvape: 외부 결제 게이트웨이 부분 장애(간헐 50% 5xx)
# =============================================================================
# Root Cause: 외부 PG(결제 게이트웨이, pg-mock)가 요청의 약 50% 를 빠르게 500 으로
#             반환하는 부분 장애 상태가 된다. payment-service 가 PG 호출에서 간헐
#             5xx 를 받아 주문의 약 절반이 실패한다. timeout(전량 지연)이 아니라
#             빠른 부분 실패가 변별점(scenario-02 와 차별).
#
# 전파 경로: PG 50% 500(빠름) → payment 호출 간헐 실패 → order 약 50% 5xx
#
# 관측 증거(2026-06-05 실측): apm 외부 호출 error rate ~50%, 빠른 5xx
#   (scenario-02 의 read-timeout 10s 지연과 대비). RCA apm error 채널이 읽음.
#
# 메커니즘: scenario-02 패턴 답습 — 실제 pg-mock(docker) 중단 후 같은 호스트 포트
#   8190 에 50% 500 / 50% 정상 200(PaymentResponse 스키마) 응답하는 stdlib HTTP
#   mock 을 올린다.
#
#   ./scenario-20-external-partial-failure.sh           # 실행
#   ./scenario-20-external-partial-failure.sh cleanup   # 원상복구(pg-mock 복원)
# =============================================================================
set -uo pipefail
# k3d: 호스트 NodePort/노드 IP(172.x) 미노출·취약 → 앱 API 는 클러스터 내부 nginx 를 앱 파드 exec 로 호출.
# (pg-mock 은 호스트 docker 컨테이너 + 호스트 python mock 이므로 그쪽 작업은 호스트 그대로 유지)
export KUBECONFIG="${KUBECONFIG:-/root/tb-kubeconfig}"
NAMESPACE="${NAMESPACE:-rca-testbed-plopvape}"
API_BASE="${API_BASE:-http://testbed-nginx-external}"
APP_POD_LABEL="${APP_POD_LABEL:-app=testbed-order}"
PG_MOCK_CONTAINER="${PG_MOCK_CONTAINER:-pg-mock}"
PG_MOCK_PORT="${PG_MOCK_PORT:-8190}"
FAIL_RATE="${FAIL_RATE:-0.5}"
LOAD_DURATION_SEC="${LOAD_DURATION_SEC:-300}"
CONCURRENT_ORDERS="${CONCURRENT_ORDERS:-15}"
ROUND_SLEEP_SEC="${ROUND_SLEEP_SEC:-4}"
MOCK_PY="/tmp/ex2-partial-pg-mock.py"
MOCK_PID_FILE="/tmp/ex2-partial-pg-mock.pid"
RESULT_LOG="/tmp/ex2-plopvape-partial.log"
CYAN='\033[0;36m'; GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
log_info(){ echo -e "${CYAN}[INFO]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_ok(){ echo -e "${GREEN}[OK]${NC}   $(date '+%Y-%m-%d %H:%M:%S') $*"; }
log_error(){ echo -e "${RED}[ERROR]${NC} $(date '+%Y-%m-%d %H:%M:%S') $*"; }

# 클러스터 내부 HTTP 호출 헬퍼 (k3d: 호스트 NodePort 미노출 → 앱 파드 exec 경유; pg-mock 호스트작업은 별개)
API_POD=""
resolve_api_pod(){ API_POD=$(kubectl -n "$NAMESPACE" get pod -l "$APP_POD_LABEL" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null); }
api_curl(){ kubectl -n "$NAMESPACE" exec "$API_POD" -- curl "$@"; }

write_mock_py(){
    cat > "$MOCK_PY" <<'PYEOF'
import json, os, random, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
FAIL_RATE = float(os.environ.get("FAIL_RATE", "0.5"))
PORT = int(os.environ.get("PG_MOCK_PORT", "8190"))
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404); self.end_headers()
    def do_POST(self):
        ln = int(self.headers.get("Content-Length", 0) or 0)
        try: self.rfile.read(ln)
        except Exception: pass
        if random.random() < FAIL_RATE:
            self.send_response(500); self.send_header("Content-Type","application/json"); self.end_headers()
            self.wfile.write(b'{"error":"PG gateway partial failure"}')
        else:
            body = json.dumps({"transaction_id":"PG-"+uuid.uuid4().hex[:8],"status":"SUCCESS","message":"ok"}).encode()
            self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers()
            self.wfile.write(body)
ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
PYEOF
}

inject(){
    echo "INJECTION_START_MS=$(date +%s%3N)"
    log_info "=== 실제 pg-mock 중단 후 부분장애(50% 500) mock 기동 ==="
    docker stop "$PG_MOCK_CONTAINER" >/dev/null 2>&1 || true
    # 포트 해제 대기
    local w=0; while ss -tlnp 2>/dev/null | grep -q ":${PG_MOCK_PORT} "; do sleep 1; w=$((w+1)); [[ $w -ge 15 ]] && break; done
    write_mock_py
    FAIL_RATE="$FAIL_RATE" PG_MOCK_PORT="$PG_MOCK_PORT" nohup python3 "$MOCK_PY" >/tmp/ex2-mock.out 2>&1 &
    echo $! > "$MOCK_PID_FILE"
    sleep 2
    local hc; hc=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "http://127.0.0.1:${PG_MOCK_PORT}/health" 2>/dev/null || echo "000")
    log_info "부분장애 mock health=$hc (FAIL_RATE=$FAIL_RATE)"
}
send_orders(){
    local round=$1 pids=()
    for i in $(seq 1 "$CONCURRENT_ORDERS"); do
        api_curl -s -o /dev/null -w "ex2-order-${round}-${i}: HTTP %{http_code}\n" --max-time 25 \
            -X POST "$API_BASE/api/orders" -H "Content-Type: application/json" \
            -d "{\"customerName\":\"ex2-${round}-${i}\",\"customerEmail\":\"ex2-${round}-${i}@test.com\",\"items\":[{\"productId\":$((i%16+1)),\"quantity\":1}]}" \
            >> "$RESULT_LOG" 2>&1 &
        pids+=($!)
    done
    for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
}
cleanup(){
    log_info "=== cleanup: 부분장애 mock 종료 + pg-mock 복원 ==="
    [[ -f "$MOCK_PID_FILE" ]] && { kill "$(cat "$MOCK_PID_FILE")" 2>/dev/null || true; rm -f "$MOCK_PID_FILE"; }
    # 잔존 리스너 정리
    pkill -f "$MOCK_PY" 2>/dev/null || true
    sleep 1
    docker start "$PG_MOCK_CONTAINER" >/dev/null 2>&1 || true
    local w=0; while :; do
        local hc; hc=$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 "http://127.0.0.1:${PG_MOCK_PORT}/health" 2>/dev/null || echo "000")
        [[ "$hc" == "200" ]] && { log_ok "pg-mock 복원 완료 (${w}s, health=$hc)"; break; }
        sleep 2; w=$((w+2)); [[ $w -ge 40 ]] && { log_error "pg-mock health 복원 대기 타임아웃"; break; }
    done
    rm -f "$MOCK_PY" "$RESULT_LOG"
    echo "INJECTION_END_MS=$(date +%s%3N)"
    log_ok "Cleanup complete"
}
main(){
    if [[ "${1:-}" == "cleanup" ]]; then cleanup; trap - EXIT; exit 0; fi
    echo "=== EX-2 plopvape: External PG Partial Failure (50% 5xx) ($(date '+%F %T')) ==="
    rm -f "$RESULT_LOG"; trap cleanup EXIT
    inject
    resolve_api_pod
    [[ -z "$API_POD" ]] && { log_error "order-service Pod 없음 — API 호출 불가"; exit 1; }
    local end_time=$(( $(date +%s) + LOAD_DURATION_SEC )) round=1
    while [[ $(date +%s) -lt $end_time ]]; do
        send_orders "$round"; log_info "round ${round} 전송(PG 부분장애 유지)"; round=$((round+1)); sleep "$ROUND_SLEEP_SEC"
    done
    [[ -f "$RESULT_LOG" ]] && { echo "--- HTTP code 분포 ---"; grep -oE 'HTTP [0-9]+' "$RESULT_LOG"|sort|uniq -c; }
}
main "$@"
