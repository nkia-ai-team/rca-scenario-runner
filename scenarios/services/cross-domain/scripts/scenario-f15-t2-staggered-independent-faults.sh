#!/usr/bin/env bash
# F15-T2: staggered independent failures. This file defines the live procedure;
# creating or validating it does not execute any injection.

set -Eeuo pipefail

export KUBECONFIG="${KUBECONFIG:-/root/tb-kubeconfig}"
COMMERCE_NS="${COMMERCE_NS:-rca-testbed-commerce}"
FOOD_NS="${FOOD_NS:-rca-testbed-food}"
COMMERCE_PG_POD="${COMMERCE_PG_POD:-testbed-postgres-0}"
FOOD_MOCK_SELECTOR="${FOOD_MOCK_SELECTOR:-app=testbed-external-pg-mock}"
LOCK_HOLD_SEC="${LOCK_HOLD_SEC:-480}"
SECOND_OFFSET_SEC="${SECOND_OFFSET_SEC:-180}"
PG429_HOLD_SEC="${PG429_HOLD_SEC:-240}"
MOCK_LOCAL_PORT="${MOCK_LOCAL_PORT:-19090}"
STATE_DIR="${STATE_DIR:-/tmp/f15-t2-staggered-independent-faults}"
LOCK_PID_FILE="$STATE_DIR/lock.pid"
PORT_FORWARD_PID_FILE="$STATE_DIR/port-forward.pid"
TIMES_FILE="$STATE_DIR/times.json"

log() { printf '[F15-T2] %s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }

mock_url() { printf 'http://127.0.0.1:%s' "$MOCK_LOCAL_PORT"; }

mock_pod() {
  kubectl -n "$FOOD_NS" get pod -l "$FOOD_MOCK_SELECTOR" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null
}

start_port_forward() {
  if curl -fsS --max-time 2 "$(mock_url)/liveness/probe" >/dev/null 2>&1; then
    return
  fi
  local pod
  pod=$(mock_pod)
  [[ -n "$pod" ]] || die 'food external-pg-mock pod not found'
  kubectl -n "$FOOD_NS" port-forward "pod/$pod" "$MOCK_LOCAL_PORT:1080" \
    >"$STATE_DIR/port-forward.log" 2>&1 &
  printf '%s\n' "$!" > "$PORT_FORWARD_PID_FILE"
  for _ in $(seq 1 20); do
    if curl -fsS --max-time 2 "$(mock_url)/liveness/probe" >/dev/null 2>&1; then
      return
    fi
    sleep 1
  done
  die 'mockserver port-forward did not become ready'
}

put_mock_expectation() {
  local status=$1 body=$2 id=$3 priority=$4
  curl -fsS --max-time 10 -X PUT "$(mock_url)/mockserver/expectation" \
    -H 'Content-Type: application/json' \
    -d "{\"id\":\"$id\",\"priority\":$priority,\"httpRequest\":{\"method\":\"POST\",\"path\":\"/pay\"},\"httpResponse\":{\"statusCode\":$status,\"headers\":{\"content-type\":[\"application/json\"]},\"body\":\"$body\"}}" \
    >/dev/null
}

restore_mock() {
  start_port_forward
  curl -fsS --max-time 10 -X PUT "$(mock_url)/mockserver/reset" >/dev/null
  put_mock_expectation 200 '{\\"status\\":\\"APPROVED\\",\\"transaction_id\\":\\"mock-tx-default\\"}' \
    'f15-t2-default-success' 0
  log 'food external PG mock restored to HTTP 200'
}

set_mock_429() {
  curl -fsS --max-time 10 -X PUT "$(mock_url)/mockserver/reset" >/dev/null
  put_mock_expectation 429 '{\\"status\\":\\"RATE_LIMITED\\"}' 'f15-t2-rate-limit' 100
  log 'food external PG mock set to HTTP 429'
}

terminate_lock_backend() {
  kubectl -n "$COMMERCE_NS" exec "$COMMERCE_PG_POD" -- \
    psql -U commerce -d commerce -v ON_ERROR_STOP=1 -tAc \
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name='f15-t2-inventory-lock' AND pid <> pg_backend_pid();" \
    >/dev/null 2>&1 || true
}

stop_pid_file() {
  local file=$1
  [[ -f "$file" ]] || return 0
  local pid
  pid=$(cat "$file")
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
  rm -f "$file"
}

cleanup() {
  local rc=${1:-0}
  trap - EXIT INT TERM
  log 'cleanup start'
  stop_pid_file "$LOCK_PID_FILE"
  terminate_lock_backend
  if kubectl -n "$FOOD_NS" get pod -l "$FOOD_MOCK_SELECTOR" >/dev/null 2>&1; then
    restore_mock || rc=1
  fi
  stop_pid_file "$PORT_FORWARD_PID_FILE"
  log "cleanup complete rc=$rc"
  return "$rc"
}

on_exit() {
  local rc=$?
  if ! cleanup "$rc"; then
    rc=1
  fi
  exit "$rc"
}

check_prerequisites() {
  command -v kubectl >/dev/null || die 'kubectl not found'
  command -v curl >/dev/null || die 'curl not found'
  kubectl -n "$COMMERCE_NS" get pod "$COMMERCE_PG_POD" >/dev/null
  [[ -n "$(mock_pod)" ]] || die 'food external-pg-mock pod not found'
  [[ "$LOCK_HOLD_SEC" =~ ^[0-9]+$ ]] || die 'LOCK_HOLD_SEC must be an integer'
  [[ "$SECOND_OFFSET_SEC" =~ ^[0-9]+$ ]] || die 'SECOND_OFFSET_SEC must be an integer'
  [[ "$PG429_HOLD_SEC" =~ ^[0-9]+$ ]] || die 'PG429_HOLD_SEC must be an integer'
  (( SECOND_OFFSET_SEC + PG429_HOLD_SEC <= LOCK_HOLD_SEC )) || \
    die 'second fault must overlap the commerce lock window'
  terminate_lock_backend
  mkdir -p "$STATE_DIR"
  start_port_forward
  restore_mock
}

start_inventory_lock() {
  log "starting commerce inventory lock for ${LOCK_HOLD_SEC}s"
  kubectl -n "$COMMERCE_NS" exec "$COMMERCE_PG_POD" -- \
    psql -U commerce -d commerce -v ON_ERROR_STOP=1 -c \
    "SET application_name='f15-t2-inventory-lock'; BEGIN; SELECT id FROM inventory_schema.inventory ORDER BY id FOR UPDATE; SELECT pg_sleep($LOCK_HOLD_SEC); ROLLBACK;" \
    >"$STATE_DIR/inventory-lock.log" 2>&1 &
  printf '%s\n' "$!" > "$LOCK_PID_FILE"

  for _ in $(seq 1 20); do
    local active
    active=$(kubectl -n "$COMMERCE_NS" exec "$COMMERCE_PG_POD" -- \
      psql -U commerce -d commerce -tAc \
      "SELECT count(*) FROM pg_stat_activity WHERE application_name='f15-t2-inventory-lock';" \
      2>/dev/null || printf '0')
    if [[ "$active" == '1' ]]; then
      log 'commerce inventory lock confirmed'
      return
    fi
    sleep 1
  done
  die 'commerce inventory lock was not confirmed'
}

run_scenario() {
  [[ ! -e "$LOCK_PID_FILE" ]] || die "stale state exists: run cleanup first ($STATE_DIR)"
  check_prerequisites
  trap on_exit EXIT
  trap 'exit 130' INT TERM

  local t1 t2
  t1=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
  start_inventory_lock
  log "t1=$t1; waiting ${SECOND_OFFSET_SEC}s before independent food fault"
  sleep "$SECOND_OFFSET_SEC"
  set_mock_429
  sleep "$PG429_HOLD_SEC"
  restore_mock

  local lock_pid
  lock_pid=$(cat "$LOCK_PID_FILE")
  wait "$lock_pid"
  rm -f "$LOCK_PID_FILE"
  t2=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
  printf '{"scenario_id":"cross-domain/f15-t2","t1":"%s","t2":"%s"}\n' \
    "$t1" "$t2" > "$TIMES_FILE"
  log "t2=$t2; scenario complete; capture contract uses [t1-2h,t2+45m]"
}

print_dry_run() {
  cat <<EOF
scenario=cross-domain/f15-t2
side_effects=false
orchestrator=scenario-runner@192.168.200.109
step.1=offset:0s,duration:${LOCK_HOLD_SEC}s,kind:database,transport:kubectl,location:commerce/${COMMERCE_PG_POD}
step.2=offset:${SECOND_OFFSET_SEC}s,duration:${PG429_HOLD_SEC}s,kind:external_mock,transport:kubectl,location:food/external-pg-mock
t1=step.1.start
t2=max(step.end)
cleanup_order=food-external-pg-429,commerce-inventory-lock
capture_window=[t1-2h,t2+45m]
required_checks=kube-context,rbac,pod-ready,mock-path,baseline-traffic,stale-state
EOF
}

case "${1:-run}" in
  cleanup)
    mkdir -p "$STATE_DIR"
    cleanup 0
    ;;
  dry-run)
    print_dry_run
    ;;
  run)
    run_scenario
    ;;
  *)
    die "unsupported mode: $1 (use run|cleanup|dry-run)"
    ;;
esac
