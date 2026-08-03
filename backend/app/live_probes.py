"""Fail-closed, read-only production probes for controller evidence.

Scenario documents may name approved check/query IDs and scalar parameters.  They
cannot provide endpoints, command arguments, SQL, selectors, or PromQL.  Those
production details are fixed here and every external client is injected, which
keeps unit tests side-effect free.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

from app.adaptive_runtime import EligibilityEvidence, EligibilityRequest
from app.coordinator import CoordinatorState
from app.observations import (
    CLOCK_SKEW_TOLERANCE_SEC,
    ApprovedQuery,
    ApprovedQueryRegistry,
    ObservationContractError,
)


KUBECONFIG = "/root/tb-kubeconfig"
EXPECTED_KUBE_CONTEXT = "kubernetes-admin@kubernetes"
EXPECTED_KUBE_NODES = frozenset({"tb-cp", "tb-w1", "tb-w2", "tb-w3"})
LOADGEN_HOST = "192.168.122.206"
LOADGEN_USER = "nkia"
LOADGEN_KEY = "/root/.ssh/tb_key"
# k6 live-summary fields, keyed by query id. This mapping is the *only*
# allowlist for loadgen observations — a second hand-maintained id set used to
# guard it, and loadgen.food_create_429_rate was added here but not there, so
# F06-P's sole success condition failed closed on every tick (2026-07-29).
LOADGEN_FIELDS = {
    "loadgen.achieved_rps": "achieved_rps",
    "loadgen.checkout_5xx_rate": "checkout_5xx_rate",
    "loadgen.write_step_status_rate": "business_nonok_rate",
    "loadgen.read_step_status_rate": "read_nonok_rate",
    "loadgen.food_create_status_rate": "business_5xx_rate",
    "loadgen.transfer_2xx_rate": "business_2xx_rate",
    # F23-R: business_409_rate is computed by the north-south monitor
    # (business_step status==409 fraction) alongside business_5xx_rate.
    "loadgen.checkout_409_rate": "business_409_rate",
    # F06-P: 하류 rate limit(429)은 4xx의 또 다른 부분집합이라
    # business_nonok_rate로는 검증 거절과 구분되지 않고, business_5xx_rate로는
    # 아예 보이지 않는다 — 앱이 하류 상태코드를 5xx로 승격하지 않고 그대로
    # 전파하기 때문이다.
    "loadgen.food_create_429_rate": "business_429_rate",
    # F17-P dual-arm reuse: direct arm 2xx rate / control arm reject rate, both
    # already emitted per business_step/read_step tagging.
    "loadgen.frozen_bypass_completed_rate": "business_2xx_rate",
    "loadgen.normal_path_reject_rate": "read_nonok_rate",
}
# Observation plane separation (2026-07-29): a loadgen observation may name a
# domain, in which case it reads that domain's *baseline* live document instead
# of the scenario's own k6 output. Without this every loadgen observation was
# only available for the one domain the scenario itself flooded, which is what
# blocked F15-G/T3/T4 and forced surge scripts to double as observation vehicles.
# Deliberately not a new id space: 10 fields x 3 domains would fork LOADGEN_FIELDS,
# and a forked allowlist is exactly what killed F06-P.
APPROVED_LOADGEN_DOMAINS = frozenset({"commerce", "core-banking", "food-delivery"})
TARGET_HEALTH_URL = "http://192.168.122.77:30080/health"
PROMETHEUS_URL = os.environ.get(
    "PROMETHEUS_URL", "http://192.168.230.119:18428/api/v1/query"
)
# Error rate is read from the trace table rather than from the APM rollup.
# 2026-07-30, same 60-minute window: agg_service_golden_signals reported 323
# requests for commerce-gateway against 6,321 root spans in otel_traces_local,
# and 1-4 requests for commerce-order / commerce-payment / food-delivery-payment
# against 226-288. The rollup is an AggregatingMergeTree whose req_count and
# error_count are plain UInt64 columns, so same-minute insert blocks discard all
# but one row — the loss factor runs 20x to 288x and varies per service. The
# surviving block held ~1 request, which made every threshold (2/5/10/30%) decide
# on whether that single sampled request happened to fail. The traces themselves
# are intact and lag only ~3s, so we count the real denominator here.
CLICKHOUSE_URL = os.environ.get("CLICKHOUSE_URL", "http://192.168.230.119:18123/")
CLICKHOUSE_USER = os.environ.get("CLICKHOUSE_USER", "lucida")
CLICKHOUSE_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "")
# 60s trailing window: it matches the update interval the timing contracts were
# verified against, and one window carries no rows in common with the next.
#
# The error test is the HTTP response status, not the span status. Measured over
# 48h: SERVER spans never carry status_code='ERROR' in this testbed — every one
# of the 3,204 ERROR spans in the last hour sat on CLIENT or INTERNAL spans,
# because the Spring instrumentation records the downstream failure on the
# outbound span and the inbound span keeps status UNSET. A span-status test would
# have returned a constant 0, which is the dead-observation class this change
# exists to remove. `http.response.status_code >= 500` is also what the scenarios
# mean by an error: 502 dominates the 48h 5xx history (10,063 of 10,430).
#
# Verified against the 2026-07-28 23:55 commerce-payment outage window:
# food-delivery-order 96.3636% (53/55), food-delivery-dispatch 35.2941% (18/51).
CLICKHOUSE_TEMPLATES = {
    "trace-service-error-rate-v1": (
        "SELECT if(count() = 0, 0, round(100.0 * countIf("
        "toUInt16OrZero(span_attributes['http.response.status_code']) >= 500"
        ") / count(), 4)) AS value "
        "FROM lucida.otel_traces_local "
        "WHERE service_name = '%s' AND span_kind = 'SERVER' "
        "AND timestamp > now() - INTERVAL 60 SECOND "
        "FORMAT JSON"
    ),
}

APPROVED_CHECK_IDS = frozenset(
    {
        "kube-context",
        "kube-node-set",
        "coordinator-clean",
        "clean-window",
        "baseline-traffic",
        # "traffic is flowing" and "transactions are succeeding" are different
        # questions; only the second one would have caught the 2026-07-28
        # missing-seed-account outage. See BASELINE_PAID_ORDERS_SQL.
        "baseline-business-success",
        "target-health",
    }
)

PROMETHEUS_TEMPLATES = {
    "kcm-node-cpu-utilization-v1": (
        'max without(grade) (kcm.node.cpu_utilization{node="%s"})'
    ),
    # 실측(2026-07-21): 메트릭명은 mem_utilization(memory_ 아님), 단위 퍼센트,
    # grade 라벨 중복은 max로 붕괴.
    "kcm-node-memory-utilization-v1": (
        'max without(grade) (kcm.node.mem_utilization{node="%s"})'
    ),
    "http-server-duration-p95-v1": (
        'histogram_quantile(0.95, sum by (le) '
        '(rate(http_server_request_duration_seconds_bucket{service_name=~"%s"}[2m])))'
    ),
    "apm-agent-percentile95-v1": (
        'max without(grade) (apm.agent.otel.java.percentile95{service_name="%s"})'
    ),
    "apm-agent-error-rate-v1": (
        'max without(grade) (apm.agent.otel.java.error_rate{service_name="%s"})'
    ),
    # The bare sum() this used until 2026-07-30 also summed `grade`, which the
    # APM pipeline fans every series out across (13 copies of one measurement —
    # see the daemon-thread note below). Pending sits at 0 at rest so the 13x
    # inflation was invisible, but it only leaves 0 during the very faults these
    # gates judge: F21-Q's `pool-is-the-bottleneck > 2` would have disqualified
    # the run on a single genuinely-pending connection. Collapse grade first,
    # then sum across pools/instances as before.
    "otel-hikari-pending-v1": (
        'sum(max without(grade) '
        '(db.client.connections.pending_requests{service_name="%s"}))'
    ),
    # Parameterized on 2026-07-28. It used to hardcode testbed-product, which
    # made the throttling signal exist for F12-H and for nothing else — F09-P
    # throttles testbed-inventory and was rejected before it reached PromQL.
    "kcm-pod-cpu-throttled-time-v1": (
        'max without(grade) (kcm.pod.cpu_throttled_time{namespace="%s",'
        'pod=~"%s-.*"})'
    ),
    # Old-gen occupancy immediately after a collection, as a fraction of the
    # pool limit. This is the GC-pressure signal: a heap that cannot be reclaimed
    # keeps climbing here even though used/committed look busy either way.
    # jvm.gc.duration is not collected, so this stands in for it (F09-H).
    "otel-jvm-old-gen-after-gc-ratio-v1": (
        'max by (service_name) (apm.agent.otel.java.jvm.memory.used_after_last_gc'
        '{service_name="%s",jvm_memory_pool_name="Tenured Gen"}) '
        '/ max by (service_name) (apm.agent.otel.java.jvm.memory.limit'
        '{service_name="%s",jvm_memory_pool_name="Tenured Gen"})'
    ),
    "kcm-workload-network-error-rate-v1": (
        'max without(grade) (kcm.pod.network_rx_error{namespace="rca-testbed-commerce",'
        'pod=~"testbed-product-.*"}) + max without(grade) '
        '(kcm.pod.network_tx_error{namespace="rca-testbed-commerce",'
        'pod=~"testbed-product-.*"})'
    ),
    # F21-Q/P: no Tomcat-thread-pool metric exists in the APM pipeline —
    # Tomcat's http-nio-*-exec worker threads are DAEMON threads, so the
    # non-daemon filter this query used until 2026-07-28 excluded exactly the
    # pool the thread-saturation scenarios (F21-P, F21-Q) are about. Measured on
    # VictoriaMetrics 119:18428 over 24h: non-daemon sits flat at 4-5 and never
    # moves, while daemon runs ~40 at rest and climbs to ~80 under load. A gate
    # of "busy threads >= 180" against the non-daemon count could never fire.
    #
    # 2026-07-30: that ~40 was the per-grade truth, but the query summed `grade`
    # away — the APM pipeline emits 13 identical copies of every series under
    # distinct grade ids, so the gate read ~570 instead of ~44. Everything
    # downstream inverted: success (>=180) held at rest, must_rule_out (<60)
    # could never fire, and recovery (<60) could never complete. States DO
    # partition the thread set, so they are still summed; grade and the instance
    # labels are collapsed with max (max-threads is per JVM — with replicas we
    # want the worst instance, not their sum). Measured after the fix: 42-45 at
    # rest for core-banking-api, 39-43 for food-delivery-order.
    "otel-jvm-daemon-thread-count-v1": (
        'max without(grade,target_id,host_name,process_pid,os_description,'
        'os_type,host_arch) (sum without(jvm_thread_state) '
        '(apm.agent.otel.java.jvm.thread.count'
        '{service_name="%s",jvm_thread_daemon="true"}))'
    ),
}
APPROVED_SERVICES = frozenset({"commerce-gateway", "commerce-order", "commerce-payment"})
APPROVED_APM_SERVICES = frozenset(
    {"commerce-gateway", "commerce-product", "commerce-order", "commerce-pricing",
     "commerce-payment", "food-delivery-payment",
     # F20-Q (food order heap pressure) + F20-P (banking transfer/account/api/
     # ledger chain observation).
     "food-delivery-order", "core-banking-transfer", "core-banking-account",
     "core-banking-api", "core-banking-ledger",
     # F02-P (live defect repair — restaurant_p95 was already dispatched by
     # F02-P's controller but missing from this allowlist) + F24-Q: restaurant
     # p95/error_rate is the root-cause discriminator signal.
     "food-delivery-restaurant"}
)
# F21-Q/P: JVM non-daemon thread sum approximates the Tomcat 200-thread pool
# (no Tomcat-specific metric exists in the pipeline — see PROMETHEUS_TEMPLATES).
APPROVED_JVM_THREAD_SERVICES = frozenset({"food-delivery-order", "core-banking-api"})
# F09-H: heap pressure on commerce order.
APPROVED_GC_SERVICES = frozenset({"commerce-order"})
# (namespace, deployment, container) allowed to be asked about CPU throttling.
THROTTLE_TARGETS = {
    ("rca-testbed-commerce", "testbed-product", "product-service"),
    ("rca-testbed-commerce", "testbed-inventory", "inventory-service"),
    # F21-P (2026-07-31): the injected cause itself — transfer is the only
    # service being throttled, so this is the observation that proves the fault
    # landed. It was missing on the first calibration run and the observation
    # read error for the whole injection; the scenario could not show its own
    # cause. test_observation_targets_are_allowlisted now fails on any such gap.
    ("rca-testbed-banking", "testbed-transfer", "transfer-service"),
}
# F19-P: Hikari pending gauge (OTel semconv db.client.connections.pending_requests,
# live-verified 2026-07-24 on VictoriaMetrics 119:18428).
# F20-P: transfer's own Hikari pool occupied by the trunc() full-scan stats
# query — the decisive "own connection pool contention" signal for F20-P.
# F02-H: commerce order's pool backing up is what separates "the storage is
# saturated" from "the application got slower" — the wait is on the DB side.
APPROVED_HIKARI_SERVICES = frozenset(
    {"food-delivery-order", "core-banking-transfer", "commerce-order"}
)
APPROVED_NODE_TARGETS = frozenset({"tb-w1", "tb-w2", "tb-w3"})
APPROVED_BUSINESS_KEYS = frozenset({"checkout", "order-1"})
APPROVED_K8S_TARGETS = {
    ("rca-testbed-commerce", "api-gateway"): "app=testbed-gateway",
    # F05-P names the gateway by its real Deployment name; the api-gateway key
    # above predates it and is the container's name, kept for older manifests.
    ("rca-testbed-commerce", "testbed-gateway"): "app=testbed-gateway",
    ("rca-testbed-commerce", "testbed-payment"): "app=testbed-payment",
    ("rca-testbed-commerce", "testbed-inventory"): "app=testbed-inventory",
    ("rca-testbed-commerce", "testbed-cart"): "app=testbed-cart",
    ("rca-testbed-commerce", "testbed-product"): "app=testbed-product",
    ("rca-testbed-commerce", "testbed-pricing"): "app=testbed-pricing",
    ("rca-testbed-commerce", "testbed-order"): "app=testbed-order",
    ("rca-testbed-commerce", "testbed-postgres"): "app=testbed-postgres",
    # F04-R (commerce shipping consumer stop) watches the Kafka StatefulSet pod
    # testbed-kafka-0 via pod_ready; the StatefulSet carries label app=testbed-kafka.
    ("rca-testbed-commerce", "testbed-kafka-0"): "app=testbed-kafka",
    ("rca-testbed-food", "testbed-mysql"): "app=testbed-mysql",
    # F15-T1 watches the food payment pod readiness through its OOM ladder.
    ("rca-testbed-food", "testbed-payment"): "app=testbed-payment",
    ("rca-testbed-banking", "testbed-oracle"): "app=testbed-oracle",
    # F17-R watches the banking transfer pod dropping NotReady under its
    # readinessProbe fault while commerce checkout degrades cross-domain.
    ("rca-testbed-banking", "testbed-transfer"): "app=testbed-transfer",
    # F19-P/S watch the food order pod while its Hikari pool saturates.
    ("rca-testbed-food", "testbed-order"): "app=testbed-order",
    # F16-H watches the user pod dropping NotReady under its readinessProbe
    # fault while the gateway fail-closes every write route with 401.
    ("rca-testbed-commerce", "testbed-user"): "app=testbed-user",
    # F21-P watches the banking api pod while its Tomcat 200 thread pool
    # saturates (109 kubectl-verified 2026-07-24: app=testbed-api).
    ("rca-testbed-banking", "testbed-api"): "app=testbed-api",
    # F14-P's recovery gate watches the ledger pod after its table-readonly
    # injection clears. Missing here through the whole 2026-08-03 batch, so
    # recovery could never be verified and the run always aged into DIRTY
    # (kubectl-verified 2026-08-03: app=testbed-ledger).
    ("rca-testbed-banking", "testbed-ledger"): "app=testbed-ledger",
    # F24-Q (+ F02-P live defect repair) watches the restaurant pod while its
    # Hikari/Tomcat pool saturates under load.north_south flood on NodePort
    # 30181 (109 k8s manifest-verified 2026-07-24: app=testbed-restaurant).
    ("rca-testbed-food", "testbed-restaurant"): "app=testbed-restaurant",
}
F12_PRODUCT_TARGET = {
    "namespace": "rca-testbed-commerce",
    "deployment": "testbed-product",
    "container": "product-service",
}
# Every Oracle lock scenario, not just F01-P. The tag used to be frozen into a
# single contract dict *and* hand-encoded as chr() codes inside the SQL, so
# F08-G and F15-G had no way to be observed at all and pointed their Oracle
# observations at the PostgreSQL probe instead — which cannot see an Oracle
# session, so the gate could never pass.
# PostgreSQL side. F01-R and F06-H are absent on purpose: their injectors now
# impersonate the real application (G6/L1), so they are observed through
# database.blocked_session_count instead of a name. The three that remain still
# tag, and F15-G was missing here — which is why its lock was invisible to its
# own controller.
APPROVED_SESSION_TAGS = frozenset({
    "rca-F02-G-batch-heavy-sql",
    "rca-F15-G-inventory-lock",
    "rca-F15-T1-inventory-lock",
})
# The manifests stopped naming the scenario in the session tag — an `rca-F01-P-*`
# identifier is a level-2 answer leak, so all three Oracle locks now impersonate a
# plausible DBA session. This allowlist kept the old names and therefore rejected
# every real tag with "Oracle session tag is not allowlisted", which left F01-P,
# F08-G and F15-G unable to verify recovery and wedged the queue with a global
# DIRTY (2026-08-03). The old names are dead: no manifest emits them.
APPROVED_ORACLE_TAGS = frozenset({
    "dba-maintenance",
})


def _oracle_string_literal(value: str) -> str:
    """Render a string as chr()||chr() so it can ride inside the nested
    printf/sqlplus shell pipeline without a single quote of its own."""
    if not value or not all(32 <= ord(ch) < 127 for ch in value):
        raise LiveProbeError("Oracle tag is not printable ASCII")
    return "||".join(f"chr({ord(ch)})" for ch in value)


def _oracle_sqlplus(query: str) -> str:
    """Build the sh -lc argument for a single-value Oracle probe.

    The container switch prints "Session altered.", so the display settings must
    already be in force when it runs. All four Oracle probes had the two lines the
    other way round, which put that sentence ahead of the digits and made the
    fullmatch check reject every reading — measured 2026-07-30: the probes had never
    once returned a value. Emitting the order from one place keeps the next probe
    from reintroducing it.
    """
    return (
        "printf 'set pages 0 feedback off heading off\\n"
        "alter session set container=FREEPDB1;\\n"
        f"{query}\\n"
        "exit;\\n' | sqlplus -s / as sysdba"
    )
MYSQL_INDEX_CONTRACT = {
    "database": "fooddelivery", "table": "menus", "index": "idx_menus_category"
}
OUTBOX_UNPUBLISHED_CONTRACT = {"namespace": "rca-testbed-banking"}
# F04-H decisive evidence: commerce order_schema.outbox_events rows the halted
# relay has not published. The banking counterpart above goes through sqlplus on
# testbed-oracle-0 and is therefore unusable here — commerce lives on
# PostgreSQL, so this one rides the same database_client as the other commerce
# probes. Baseline sits near zero because the relay drains every 2s
# (OutboxRelay @Scheduled), which is what makes a monotone climb decisive.
COMMERCE_OUTBOX_UNPUBLISHED_CONTRACT = {
    "db_host": "192.168.122.77",
    "db_port": 30432,
    "db_name": "commerce",
    "db_user": "commerce",
    "schema": "order_schema",
    "table": "outbox_events",
}
COMMERCE_OUTBOX_UNPUBLISHED_SQL = (
    "SELECT count(*) AS unpublished_count FROM order_schema.outbox_events "
    "WHERE published_at IS NULL"
)
# Preflight: did a real business transaction complete recently? "Load is
# flowing" (baseline-traffic) does not answer that — it only proves k6 is
# running, and k6 keeps running happily while every checkout fails.
#
# 2026-07-28: commerce checkout failed 100% for a full day because the banking
# seed account 'commerce-merchant' went missing in the Oracle re-seed. Nothing
# caught it. Scenarios that certify damage as "checkout 5xx >= 5%" were
# trivially true with no injection at all, and scenarios that require a healthy
# checkout were trivially false. A whole day of runs would have been fiction.
#
# A PAID order is the widest single assertion available: it clears order ->
# payment -> core-banking transfer, so one query covers the cross-domain path
# that no per-service health endpoint sees.
BASELINE_PAID_ORDERS_SQL = (
    "SELECT count(*) AS paid_count FROM order_schema.orders "
    "WHERE status = 'PAID' AND created_at >= now() - interval '5 minutes'"
)
# Baseline commerce runs continuously and yields ~24 PAID orders/min (measured
# 2026-07-28: 245 in 10 minutes). Five in five minutes is a ~96% drop — far
# below normal jitter, so this trips on a broken path, not on a slow one.
BASELINE_PAID_ORDERS_MIN = 5
HOST_PROBE_CONTRACTS = {
    "F02-H": ("192.168.122.184", "fio", "/opt/local-path-provisioner/pvc-5d71e22a-1225-4505-a7cc-5cf29dad4cf5_rca-testbed-commerce_pgdata-testbed-postgres-0"),
    "F10-R": ("192.168.122.184", "watermark", "/opt/local-path-provisioner/pvc-5d71e22a-1225-4505-a7cc-5cf29dad4cf5_rca-testbed-commerce_pgdata-testbed-postgres-0"),
    "F10-H": ("192.168.122.14", "fio", "/opt/local-path-provisioner/pvc-3439d85f-f921-4b19-8808-c679506a31dd_rca-testbed-food_mysqldata-testbed-mysql-0"),
    # 2026-07-28: Oracle moved to tb-w2 when the domain-worker placement was
    # pinned with nodeSelector, and its local-path PV had to be reprovisioned —
    # so both the host and the PVC uuid changed. The old pair pointed at a
    # directory that no longer exists on a node the injection never touches.
    "F10-P": ("192.168.122.11", "fio", "/opt/local-path-provisioner/pvc-2c369013-b180-417a-9eda-da922c78b6ee_rca-testbed-banking_oracledata-testbed-oracle-0"),
    "F15-P": ("192.168.122.11", "pressure", ""),
}
HOST_PROBE_SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
scenario="$1"; mode="$2"; target="$3"
state_root=/var/lib/lucida/scenario-profile-state
pidfile="$state_root/${scenario}.pid"
active=false; artifact=false; used=0; io_util=0
if [[ -s "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then active=true; fi
case "$mode" in
 fio) [[ -e "$target/.lucida-${scenario}-fio" ]] && artifact=true ;;
 watermark) [[ -e "$target/.lucida-${scenario}-watermark" ]] && artifact=true; $artifact && active=true ;;
 pressure) ;;
 *) exit 2 ;;
esac
if [[ -n "$target" ]]; then used=$(df -P "$target" | awk 'NR==2{gsub(/%/,"",$5); print $5}'); fi
# Disk busy percentage for the device backing $target, from /proc/diskstats
# io_ticks (ms spent with I/O in flight). Partitions report io_ticks
# unreliably, so resolve the parent disk first.
# Averaged since the previous probe call (state file below): a 1s spot sample
# swung 41..98 tick-to-tick under constant-rate fio and broke every
# 3-consecutive-tick judgment while the device stayed saturated (batch #25,
# F10-P). The interval average measures what the judgment actually asks --
# "was the device busy over this tick" -- with no added probe latency. First
# call, device change, or a stale (>120s) interval falls back to a 1s sample.
if [[ -n "$target" ]]; then
  src=$(df -P "$target" | awk 'NR==2{print $1}')
  dev=$(lsblk -no PKNAME "$src" 2>/dev/null | head -1)
  [[ -z "$dev" ]] && dev=$(basename "$src")
  iostate="$state_root/${scenario}.iosample"
  now_ms=$(date +%s%3N)
  t1=$(awk -v d="$dev" '$3==d{print $13}' /proc/diskstats)
  pdev=""; pticks=""; pms=""
  { [[ -s "$iostate" ]] && read -r pdev pticks pms < "$iostate"; } || true
  if [[ -n "$t1" && "$pdev" == "$dev" && -n "$pticks" && -n "$pms" ]] \
     && (( now_ms > pms )) && (( now_ms - pms <= 120000 )) && (( t1 >= pticks )); then
    io_util=$(( (t1 - pticks) * 100 / (now_ms - pms) ))
  elif [[ -n "$t1" ]]; then
    t0=$t1
    sleep 1
    t1=$(awk -v d="$dev" '$3==d{print $13}' /proc/diskstats)
    if [[ -n "$t1" && "$t1" -ge "$t0" ]]; then
      io_util=$(( (t1 - t0) / 10 ))
    fi
    now_ms=$(date +%s%3N)
  fi
  (( io_util > 100 )) && io_util=100
  if [[ -n "$t1" ]]; then
    # state_root is created by the executors; a host where none ever ran must
    # degrade to the 1s fallback, not fail the whole probe.
    { printf '%s %s %s\n' "$dev" "$t1" "$now_ms" > "$iostate"; } 2>/dev/null || true
  fi
fi
clean=true; $active && clean=false; $artifact && clean=false
printf '{"active":%s,"clean":%s,"filesystem_used_percent":%s,"disk_io_utilization":%s}\n' \
  "$active" "$clean" "$used" "$io_util"
'''
TAGGED_SESSION_SQL = (
    "SELECT count(*) AS tagged_count FROM pg_stat_activity "
    "WHERE application_name = current_setting('lucida.scenario_tag')"
)
# Successor to TAGGED_SESSION_SQL for the PostgreSQL lock scenarios (F01-R,
# F06-H). Those injectors no longer wear a scenario-encoded application_name —
# the tag was the answer written in plain text in the captured data (quality
# charter L1), so the injecting session now presents itself as "PostgreSQL JDBC
# Driver" exactly like the app. A name-based count therefore always returns 0.
#
# The replacement counts the injection's *victims* instead of its signature:
# sessions that are blocked (pg_blocking_pids) while touching the target
# relation. This is strictly better evidence — a held lock nobody waits on is
# not an incident (charter G3), so what the controller needs to confirm is that
# the lock actually bites. Covers both lock scopes: a row-lock waiter blocks on
# transactionid while holding a granted relation lock on the target, and a
# table-lock waiter blocks on the relation itself. Both match on l.relation.
BLOCKED_SESSION_SQL = (
    "SELECT count(*) AS blocked_count FROM pg_stat_activity a "
    "WHERE cardinality(pg_blocking_pids(a.pid)) > 0 "
    "AND EXISTS (SELECT 1 FROM pg_locks l WHERE l.pid = a.pid "
    "AND l.relation = to_regclass(current_setting('lucida.lock_relation')))"
)
# Only relations an approved db.lock level actually targets may be probed.
BLOCKED_SESSION_RELATIONS = frozenset(
    {"inventory_schema.inventory", "payment_schema.payments"}
)
# F14-P's reconciliation window. Pinned so a widened window cannot quietly turn a
# stale backlog into a fresh "damage" reading.
LEDGER_UNMATCHED_CONTRACT = {"window_minutes": 5, "grace_seconds": 60}
INDEX_PRESENT_SQL = (
    "SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_indexes "
    "WHERE schemaname = current_setting('lucida.index_schema') "
    "AND tablename = current_setting('lucida.index_table') "
    "AND indexname = current_setting('lucida.index_name')) THEN 1 ELSE 0 END"
)
PAYMENT_DUPLICATE_SINCE_T1_SQL = (
    "SELECT count(*) AS duplicate_count FROM (SELECT order_id "
    "FROM payment_schema.payments WHERE created_at >= "
    "(current_setting('lucida.payment_t1')::timestamptz AT TIME ZONE 'UTC') "
    "GROUP BY order_id HAVING count(*) > 1) AS duplicates"
)
# F20-R decisive evidence: count of backends actively running a query for
# more than 2s on the shared commerce PostgreSQL instance — the ground truth
# for "the instance is busy with slow scans" that node_cpu_utilization (node-
# level proxy) can't isolate. Instance-wide (not schema-filtered) because the
# cross-schema oison signature is exactly that unrelated schemas' backends
# also show up as long-running while order-service floods it with full scans.
PG_SLOW_ACTIVE_QUERY_SQL = (
    "SELECT count(*) AS slow_active_count FROM pg_stat_activity "
    "WHERE state = 'active' AND now() - query_start > interval '2 seconds'"
)
PG_SLOW_ACTIVE_QUERY_CONTRACT = {
    "db_host": "192.168.122.77",
    "db_port": 30432,
    "db_name": "commerce",
    "db_user": "commerce",
}
INDEX_PRESENT_CONTRACT = {
    "db_host": "192.168.122.77",
    "db_port": 30432,
    "db_name": "commerce",
    "db_user": "commerce",
    "schema": "product_schema",
    "table": "products",
    "index": "idx_products_name",
}
# F23-R decisive evidence: number of commerce products at zero stock. Normal
# consumption saws between 0 and non-zero as ReconciliationBatch restocks
# every 10 minutes; a halted batch lets this climb and stay pinned.
INVENTORY_STOCK_CONTRACT = {
    "db_host": "192.168.122.77",
    "db_port": 30432,
    "db_name": "commerce",
    "db_user": "commerce",
    "schema": "inventory_schema",
    "table": "inventory",
}
INVENTORY_ZERO_STOCK_SQL = (
    "SELECT count(*) AS zero_stock_count FROM inventory_schema.inventory WHERE stock = 0"
)
# F23-R must_rule_out companion: RESTOCK movement rows in the last 12 minutes
# (batch period 10m + margin) — zero rows means the reconciliation batch has
# stopped running, not merely that stock hasn't hit zero yet.
RESTOCK_MOVEMENT_CONTRACT = {
    "db_host": "192.168.122.77",
    "db_port": 30432,
    "db_name": "commerce",
    "db_user": "commerce",
    "schema": "inventory_schema",
    "table": "inventory_movements",
    "movement_type": "RESTOCK",
    "window_minutes": 12,
}
RESTOCK_MOVEMENT_SQL = (
    "SELECT count(*) AS restock_count FROM inventory_schema.inventory_movements "
    "WHERE movement_type = 'RESTOCK' AND created_at >= now() - interval '12 minutes'"
)
DEPLOYMENT_REPLICAS_CONTRACT = {
    "namespace": "rca-testbed-commerce",
    "deployment": "testbed-shipping",
}
APPROVED_DEPLOYMENT_REPLICA_TARGETS = frozenset(
    {
        ("rca-testbed-commerce", "testbed-shipping"),
        ("rca-testbed-commerce", "testbed-payment"),
        ("rca-testbed-commerce", "testbed-product"),
        ("rca-testbed-banking", "testbed-transfer"),
        ("rca-testbed-commerce", "testbed-user"),
        # F05-P watches gateway availableReplicas collapse while tb-w1 drains.
        # Unlisted until the widened allowlist guard flagged it (2026-08-03) —
        # the scenario skipped for other reasons in the batch, so this probe
        # had never actually run.
        ("rca-testbed-commerce", "testbed-gateway"),
    }
)
F05_PAYMENT_TARGET = {
    "namespace": "rca-testbed-commerce",
    "deployment": "testbed-payment",
    "container": "payment-service",
}
# F15-T1 drives the food-delivery payment container through the same OOM
# observation set; only the pod-status queries are shared with F05 — the
# resources/liveness match probes stay commerce-only.
F15_FOOD_PAYMENT_TARGET = {
    "namespace": "rca-testbed-food",
    "deployment": "testbed-payment",
    "container": "payment-service",
}
# F17-R must_rule_out: a rising transfer restart count would mean crashloop,
# not the injected readiness fault — only the restart-count query is shared.
F17_TRANSFER_TARGET = {
    "namespace": "rca-testbed-banking",
    "deployment": "testbed-transfer",
    "container": "transfer-service",
}
# F20-Q decisive evidence: the food order-service container climbing toward
# its 1Gi memory limit as the unpaged/GROUP-BY-DATE() slowquery journeys load
# unbounded result sets into the JVM heap. Memory + restart-count + OOM
# queries are shared with the F05/F15 payment OOM ladder shape.
F20_FOOD_ORDER_TARGET = {
    "namespace": "rca-testbed-food",
    "deployment": "testbed-order",
    "container": "order-service",
}
# F25-H watches the commerce PostgreSQL StatefulSet pod through the same OOM
# observation set as F05/F15/F20 — pod label selector is kind-agnostic
# (app=testbed-postgres, see APPROVED_K8S_TARGETS), so no new dispatch logic
# is needed beyond this target allowlist entry.
F25_H_POSTGRES_TARGET = {
    "namespace": "rca-testbed-commerce",
    "deployment": "testbed-postgres",
    "container": "postgres",
}
# F05-P must_rule_out: a rising kafka restart count would mean the broker is
# crashlooping rather than the node draining — restart-count query only.
F05_KAFKA_TARGET = {
    "namespace": "rca-testbed-commerce",
    "deployment": "testbed-kafka",
    "container": "kafka",
}
# F09-H must_rule_out: a rising order restart count would mean crashloop, not
# the injected heap pressure — restart-count query only.
F09_ORDER_TARGET = {
    "namespace": "rca-testbed-commerce",
    "deployment": "testbed-order",
    "container": "order-service",
}
F05_PAYMENT_BASELINE_RESOURCES = {
    "requests": {"cpu": "200m", "memory": "512Mi"},
    "limits": {"cpu": "500m", "memory": "1Gi"},
}
F05_PAYMENT_BASELINE_LIVENESS = {
    "httpGet": {"path": "/actuator/health", "port": 8083, "scheme": "HTTP"},
    "timeoutSeconds": 3,
    "periodSeconds": 15,
    "successThreshold": 1,
    "failureThreshold": 5,
}
KAFKA_LAG_CONTRACT = {
    "namespace": "rca-testbed-commerce",
    "pod": "testbed-kafka-0",
    "bootstrap_server": "localhost:9092",
    "consumer_group": "shipping-service",
}
# F18-P proves the ledger consumer lag stays flat while the outbox relay is
# halted — the discriminator against F04-R (consumer stop, lag grows).
BANKING_KAFKA_LAG_CONTRACT = {
    "namespace": "rca-testbed-banking",
    "pod": "testbed-kafka-0",
    "bootstrap_server": "localhost:9092",
    "consumer_group": "ledger-service",
}
APPROVED_KAFKA_LAG_CONTRACTS = (KAFKA_LAG_CONTRACT, BANKING_KAFKA_LAG_CONTRACT)


class LiveProbeError(RuntimeError):
    """A read-only producer could not prove that evidence is usable."""


class HttpClient(Protocol):
    def __call__(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float,
    ) -> Mapping[str, Any]: ...


class DatabaseClient(Protocol):
    def __call__(
        self,
        sql: str,
        parameters: tuple[str, ...],
        *,
        credentials: Mapping[str, str],
    ) -> Mapping[str, Any]: ...


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]
Clock = Callable[[], datetime]


@dataclass(frozen=True)
class ProbePaths:
    coordinator: Path = Path("/app/state/coordinator.json")
    runs: Path = Path("/var/lib/lucida/scenario-runs")
    baseline_status: Path = Path("/app/state/loadgen/baseline-status.json")
    loadgen_summary: Path = Path("/app/state/loadgen/latest-summary.json")
    # Per-domain baseline live documents, published continuously by the resident
    # loadgen units (see testbed-services docs/spec-scenario-observation-plane.md).
    # The filename carries the domain: baseline-<domain>-live.json.
    baseline_summary_dir: Path = Path("/app/state/loadgen")
    capture_root: Path = Path("/var/lib/lucida/scenario-runs")
    profile_state: Path = Path("/var/lib/lucida/scenario-profile-state")


def _default_process(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), **kwargs)


class LiveProbeSet:
    """Produce baseline eligibility and the seven approved observations."""

    def __init__(
        self,
        *,
        process_runner: ProcessRunner = _default_process,
        http_client: HttpClient,
        database_client: DatabaseClient,
        database_credentials: Mapping[str, str],
        run_id: str | None = None,
        scenario_id: str | None = None,
        clock: Clock | None = None,
        paths: ProbePaths = ProbePaths(),
    ) -> None:
        self.process_runner = process_runner
        self.http_client = http_client
        self.database_client = database_client
        self.database_credentials = dict(database_credentials)
        self.run_id = run_id
        self.scenario_id = scenario_id
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.paths = paths

    def inspect(self, request: EligibilityRequest) -> EligibilityEvidence:
        unknown = set(request.checks) - APPROVED_CHECK_IDS
        if unknown:
            raise ObservationContractError(f"unknown approved check ids: {sorted(unknown)}")
        now = _aware(self.clock())
        window_start = _aware(request.requested_at) - timedelta(seconds=request.clean_window_sec)
        overlap_ids, coordinator_clean = self._overlap_evidence(
            window_start, _aware(request.requested_at), exclude_run_id=request.run_id
        )
        checks = {
            "kube-context": self._kube_context_ok,
            "kube-node-set": self._kube_nodes_ok,
            "coordinator-clean": lambda: coordinator_clean,
            "clean-window": lambda: not overlap_ids,
            "baseline-traffic": self._baseline_active,
            "baseline-business-success": self._baseline_business_succeeds,
            "target-health": self._target_healthy,
        }
        results: dict[str, bool] = {}
        errors: dict[str, str] = {}
        for check in request.checks:
            try:
                results[check] = checks[check]() is True
            except Exception as error:
                # Preserve the reason instead of collapsing it into a bare
                # False — an exception here is a broken probe or a transient
                # transport failure, not evidence the precondition is unmet.
                results[check] = False
                errors[check] = f"{type(error).__name__}: {error}"
        baseline_active = (
            results["baseline-traffic"]
            if "baseline-traffic" in results
            else self._baseline_active()
        )
        return EligibilityEvidence(
            checked_at=now,
            source="live-probes:v1",
            quality="good",
            check_results=results,
            check_errors=errors,
            clean_window_start=window_start,
            clean_window_end=_aware(request.requested_at),
            overlapping_run_ids=sorted(overlap_ids),
            baseline_active=baseline_active,
        )

    def observe(self, query: ApprovedQuery) -> Mapping[str, Any]:
        now = _aware(self.clock())
        try:
            value, observed_at, source = {
                "loadgen_summary": self._loadgen_observation,
                "http_probe": self._http_observation,
                "prometheus": self._prometheus_observation,
                "clickhouse": self._clickhouse_observation,
                "kubernetes": self._kubernetes_observation,
                "database": self._database_observation,
                "host_probe": self._host_observation,
                "business_probe": self._business_observation,
                "capture_status": self._capture_observation,
            }[query.adapter](query)
            return {
                "value": value,
                "observed_at": _format_utc(observed_at),
                "source": source,
                "quality": "good",
            }
        except Exception as error:
            # Persist the failure detail (exit code + stderr tail for subprocess
            # errors) — without it, probe-burst aborts are undiagnosable from
            # tick records (observed 2026-07-21/22 across F12-H·F06-G·F05-H).
            detail = str(error)
            if isinstance(error, subprocess.CalledProcessError):
                stderr = (error.stderr or "").strip() if isinstance(error.stderr, str) else ""
                detail = f"exit {error.returncode}: {stderr[-200:] or 'no stderr'}"
            elif isinstance(error, subprocess.TimeoutExpired):
                detail = f"timeout after {error.timeout}s"
            return {
                "value": None,
                "observed_at": _format_utc(now),
                "source": f"live-probes:{query.query_id}:{type(error).__name__}",
                "quality": "error",
                "error": detail[:300],
            }

    def _kubectl(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.process_runner(
            ["kubectl", "--kubeconfig", KUBECONFIG, *args],
            check=True,
            capture_output=True,
            text=True,
            env=_read_only_environment(),
            timeout=15,
        )

    def _kube_context_ok(self) -> bool:
        result = self._kubectl("config", "current-context")
        return result.stdout.strip() == EXPECTED_KUBE_CONTEXT

    def _kube_nodes_ok(self) -> bool:
        result = self._kubectl("get", "nodes", "-o", "json")
        document = json.loads(result.stdout)
        names = {item["metadata"]["name"] for item in document["items"]}
        return names == EXPECTED_KUBE_NODES

    def _baseline_active(self) -> bool:
        if self.paths.baseline_status.is_file():
            document = _read_json(self.paths.baseline_status)
            observed_at = _parse_time(document["observed_at"])
            if (_aware(self.clock()) - observed_at).total_seconds() > 60:
                return False
            return document.get("unit") == "loadgen-commerce" and document.get("active") is True
        result = self.process_runner(
            [
                "ssh",
                "-i",
                LOADGEN_KEY,
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "ConnectTimeout=10",
                f"{LOADGEN_USER}@{LOADGEN_HOST}",
                "systemctl is-active loadgen-commerce",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=_read_only_environment(),
            timeout=15,
        )
        return result.stdout.strip() == "active"

    def _baseline_business_succeeds(self) -> bool:
        response = self.database_client(
            BASELINE_PAID_ORDERS_SQL, (), credentials=self.database_credentials,
        )
        count = response.get("paid_count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise LiveProbeError("baseline paid-order count is invalid")
        return count >= BASELINE_PAID_ORDERS_MIN

    def _target_healthy(self) -> bool:
        response = self.http_client("GET", TARGET_HEALTH_URL, timeout=5.0)
        return 200 <= int(response["status"]) < 300

    def _overlap_evidence(
        self, start: datetime, end: datetime, *, exclude_run_id: str
    ) -> tuple[set[str], bool]:
        overlaps: set[str] = set()
        state = CoordinatorState()
        if self.paths.coordinator.is_file():
            state = CoordinatorState.model_validate(_read_json(self.paths.coordinator))
        coordinator_clean = (
            state.dirty_run is None
            and (state.active_lease is None or state.active_lease.run_id == exclude_run_id)
        )
        for current in (state.active_lease, state.dirty_run):
            if current is not None and current.run_id != exclude_run_id:
                overlaps.add(current.run_id)
        if self.paths.runs.is_dir():
            for run_dir in self.paths.runs.iterdir():
                if not run_dir.is_dir() or run_dir.name == exclude_run_id:
                    continue
                # Not every directory under the runs root is a run. The topology
                # collector keeps its per-cycle store in cycle-topology/ here, and
                # _run_intervals' fail-safe treats a directory it cannot read a
                # timeline from as an open interval — so from 2026-07-26 that one
                # directory silently blocked the clean-window gate for every
                # scenario, forever. The fail-safe is right for a run whose
                # timeline is missing; it must not apply to something that never
                # was one.
                if not _is_run_directory(run_dir):
                    continue
                # Excused failed attempt (mode-aware retry, 2026-07-20): its short,
                # cleanly-recovered residue is accepted inside the shortened window.
                if (run_dir / "clean-window-excused.json").is_file():
                    continue
                for interval in _run_intervals(run_dir):
                    if _intersects(interval[0], interval[1], start, end):
                        overlaps.add(run_dir.name)
                        break
        return overlaps, coordinator_clean

    def _loadgen_observation(self, query: ApprovedQuery) -> tuple[float, datetime, str]:
        if query.query_id not in LOADGEN_FIELDS:
            raise LiveProbeError("unsupported loadgen query")
        if set(query.parameters) - {"domain"}:
            raise LiveProbeError("unsupported loadgen query")
        domain = query.parameters.get("domain")
        # No domain means the historical behaviour: read the scenario's own k6
        # output. The 43 live controllers pass no parameters and are untouched.
        document = (
            self._baseline_live_document(domain) if domain else self._loadgen_live_document()
        )
        field = LOADGEN_FIELDS[query.query_id]
        value = document.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise LiveProbeError(f"k6 {field} is invalid")
        if field.endswith("_rate") and value > 1:
            raise LiveProbeError(f"k6 {field} is outside [0,1]")
        source = f"k6:baseline:{domain}:{field}" if domain else f"k6:{field}"
        return float(value), _parse_time(document["observed_at"]), source

    def _http_observation(self, query: ApprovedQuery) -> tuple[int, datetime, str]:
        if query.parameters:
            raise LiveProbeError("unsupported http query")
        if query.query_id == "http.target_health":
            response = self.http_client("GET", TARGET_HEALTH_URL, timeout=5.0)
            status = int(response["status"])
            if not 0 <= status <= 599:
                raise LiveProbeError("target health status is outside the approved range")
            return status, _response_time(response, self.clock), "http:target-health"
        if query.query_id != "http.entry_health":
            raise LiveProbeError("unsupported http query")
        document = self._loadgen_live_document()
        status = document.get("entry_status")
        if isinstance(status, bool) or not isinstance(status, int) or not 0 <= status <= 599:
            raise LiveProbeError("checkout entry status is unavailable")
        return status, _parse_time(document["observed_at"]), "k6:checkout-entry-status"

    def _prometheus_observation(self, query: ApprovedQuery) -> tuple[float, datetime, str]:
        if query.template_id not in PROMETHEUS_TEMPLATES:
            raise LiveProbeError("unsupported prometheus query")
        if query.query_id == "prometheus.user_p95":
            if set(query.parameters) - {"service_name"}:
                raise LiveProbeError("prometheus parameters do not match the approved template")
            service = query.parameters.get("service_name", "commerce-gateway")
            if service not in APPROVED_SERVICES:
                raise LiveProbeError("service_name is not allowlisted")
            promql = PROMETHEUS_TEMPLATES[query.template_id] % service
        elif query.query_id in {
            "prometheus.apm_service_p95", "prometheus.apm_service_error_rate"
        }:
            if set(query.parameters) != {"service_name"}:
                raise LiveProbeError("APM service query requires the fixed service parameter")
            service = query.parameters["service_name"]
            if service not in APPROVED_APM_SERVICES:
                raise LiveProbeError("APM service_name is not allowlisted")
            promql = PROMETHEUS_TEMPLATES[query.template_id] % service
        elif query.query_id == "prometheus.hikari_pending_connections":
            if set(query.parameters) != {"service_name"}:
                raise LiveProbeError("Hikari pending query requires the fixed service parameter")
            service = query.parameters["service_name"]
            if service not in APPROVED_HIKARI_SERVICES:
                raise LiveProbeError("Hikari service_name is not allowlisted")
            promql = PROMETHEUS_TEMPLATES[query.template_id] % service
        elif query.query_id == "prometheus.jvm_daemon_thread_count":
            if set(query.parameters) != {"service_name"}:
                raise LiveProbeError("JVM thread query requires the fixed service parameter")
            service = query.parameters["service_name"]
            if service not in APPROVED_JVM_THREAD_SERVICES:
                raise LiveProbeError("JVM thread service_name is not allowlisted")
            promql = PROMETHEUS_TEMPLATES[query.template_id] % service
        elif query.query_id == "prometheus.container_cpu_throttled_time":
            if set(query.parameters) != {"namespace", "deployment", "container"}:
                raise LiveProbeError("CPU throttle query requires the fixed target parameters")
            target = (
                query.parameters["namespace"],
                query.parameters["deployment"],
                query.parameters["container"],
            )
            if target not in THROTTLE_TARGETS:
                raise LiveProbeError("CPU throttle target is not allowlisted")
            promql = PROMETHEUS_TEMPLATES[query.template_id] % (target[0], target[1])
        elif query.query_id == "prometheus.jvm_old_gen_after_gc_ratio":
            if set(query.parameters) != {"service_name"}:
                raise LiveProbeError("GC ratio query requires the fixed service parameter")
            service = query.parameters["service_name"]
            if service not in APPROVED_GC_SERVICES:
                raise LiveProbeError("GC service_name is not allowlisted")
            promql = PROMETHEUS_TEMPLATES[query.template_id] % (service, service)
        elif query.query_id == "prometheus.pod_network_error_rate":
            expected = {
                "namespace": F12_PRODUCT_TARGET["namespace"],
                "deployment": F12_PRODUCT_TARGET["deployment"],
            }
            if dict(query.parameters) != expected:
                raise LiveProbeError("network error target is not allowlisted")
            promql = PROMETHEUS_TEMPLATES[query.template_id]
        elif query.query_id in {
            "prometheus.node_cpu_utilization", "prometheus.node_memory_utilization"
        }:
            if set(query.parameters) != {"node"}:
                raise LiveProbeError("node utilization query requires the fixed node parameter")
            node = query.parameters["node"]
            if node not in APPROVED_NODE_TARGETS:
                raise LiveProbeError("node is not allowlisted")
            promql = PROMETHEUS_TEMPLATES[query.template_id] % node
        else:
            raise LiveProbeError("unsupported prometheus query")
        body = urlencode({"query": promql}).encode("ascii")
        response = self.http_client(
            "POST",
            PROMETHEUS_URL,
            body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10.0,
        )
        payload = _response_json(response)
        if payload.get("status") not in {None, "success"}:
            raise LiveProbeError("prometheus query did not succeed")
        result = payload["data"]["result"]
        if len(result) != 1:
            raise LiveProbeError("prometheus query requires exactly one series")
        timestamp, value = result[0]["value"]
        numeric = float(value)
        timestamp_value = float(timestamp)
        if not math.isfinite(numeric) or numeric < 0 or not math.isfinite(timestamp_value):
            raise LiveProbeError("prometheus query returned an invalid scalar")
        return (
            numeric,
            datetime.fromtimestamp(timestamp_value, timezone.utc),
            f"prometheus:{query.template_id}",
        )

    def _clickhouse_observation(self, query: ApprovedQuery) -> tuple[float, datetime, str]:
        if query.template_id not in CLICKHOUSE_TEMPLATES:
            raise LiveProbeError("unsupported clickhouse query")
        if set(query.parameters) != {"service_name"}:
            raise LiveProbeError("clickhouse query requires the fixed service parameter")
        service = query.parameters["service_name"]
        if service not in APPROVED_APM_SERVICES:
            raise LiveProbeError("clickhouse service_name is not allowlisted")
        # The allowlist is the boundary that keeps scenario input out of the SQL,
        # exactly as APPROVED_SERVICES does for PromQL.
        sql = CLICKHOUSE_TEMPLATES[query.template_id] % service
        response = self.http_client(
            "POST",
            CLICKHOUSE_URL,
            body=sql.encode("utf-8"),
            headers={
                "X-ClickHouse-User": CLICKHOUSE_USER,
                "X-ClickHouse-Key": CLICKHOUSE_PASSWORD,
                "Content-Type": "text/plain; charset=utf-8",
            },
            timeout=10.0,
        )
        payload = _response_json(response)
        rows = payload.get("data")
        if not isinstance(rows, list) or len(rows) != 1:
            raise LiveProbeError("clickhouse query requires exactly one row")
        numeric = float(rows[0]["value"])
        if not math.isfinite(numeric) or numeric < 0 or numeric > 100:
            raise LiveProbeError("clickhouse query returned an invalid percentage")
        # The window is trailing and evaluated server-side, so the observation is
        # current as of the request rather than of some upstream batch boundary.
        return (
            numeric,
            _aware(self.clock()),
            f"clickhouse:{query.template_id}",
        )

    def _kubernetes_observation(self, query: ApprovedQuery) -> tuple[Any, datetime, str]:
        f05_query_ids = {
            "kubernetes.container_restart_count",
            "kubernetes.container_last_termination_reason",
            "kubernetes.container_oom_killed",
            "kubernetes.container_resources_match",
            "kubernetes.container_liveness_probe_match",
            "kubernetes.container_memory_current_bytes",
            "kubernetes.container_memory_limit_bytes",
            # 외부 레지스트리(testbed-services queries.json)의 정본 id — 위
            # container_* 쌍과 동일 의미론의 별칭(F05-R/F05-H 컨트롤러가 참조).
            "kubernetes.deployment_resources_match_baseline",
            "kubernetes.deployment_liveness_probe_matches_baseline",
        }
        if query.query_id in f05_query_ids:
            parameters = dict(query.parameters)
            food_shared_ids = {
                "kubernetes.container_restart_count",
                "kubernetes.container_last_termination_reason",
                "kubernetes.container_oom_killed",
            }
            if parameters == F05_PAYMENT_TARGET:
                target = F05_PAYMENT_TARGET
            elif parameters == F15_FOOD_PAYMENT_TARGET and query.query_id in food_shared_ids:
                target = F15_FOOD_PAYMENT_TARGET
            elif parameters == F17_TRANSFER_TARGET and query.query_id == "kubernetes.container_restart_count":
                target = F17_TRANSFER_TARGET
            elif parameters == F05_KAFKA_TARGET and query.query_id == "kubernetes.container_restart_count":
                target = F05_KAFKA_TARGET
            elif parameters == F09_ORDER_TARGET and query.query_id == "kubernetes.container_restart_count":
                target = F09_ORDER_TARGET
            elif parameters == F20_FOOD_ORDER_TARGET and query.query_id in {
                "kubernetes.container_memory_current_bytes",
                "kubernetes.container_memory_limit_bytes",
                "kubernetes.container_restart_count",
                "kubernetes.container_last_termination_reason",
                "kubernetes.container_oom_killed",
            }:
                target = F20_FOOD_ORDER_TARGET
            elif parameters == F25_H_POSTGRES_TARGET and query.query_id in {
                "kubernetes.container_memory_current_bytes",
                "kubernetes.container_memory_limit_bytes",
                "kubernetes.container_restart_count",
                "kubernetes.container_last_termination_reason",
                "kubernetes.container_oom_killed",
            }:
                target = F25_H_POSTGRES_TARGET
            else:
                raise LiveProbeError("payment container target is not allowlisted")
            namespace = target["namespace"]
            deployment = target["deployment"]
            container = target["container"]
            if query.query_id in {
                "kubernetes.container_resources_match",
                "kubernetes.container_liveness_probe_match",
                "kubernetes.deployment_resources_match_baseline",
                "kubernetes.deployment_liveness_probe_matches_baseline",
            }:
                result = self._kubectl(
                    "get", "deployment", deployment, "--namespace", namespace, "-o", "json",
                )
                document = json.loads(result.stdout)
                containers = document.get("spec", {}).get("template", {}).get("spec", {}).get(
                    "containers", []
                )
                matches = [item for item in containers if item.get("name") == container]
                if len(matches) != 1:
                    raise LiveProbeError("payment deployment container is missing or ambiguous")
                is_resources = query.query_id in {
                    "kubernetes.container_resources_match",
                    "kubernetes.deployment_resources_match_baseline",
                }
                expected = (
                    F05_PAYMENT_BASELINE_RESOURCES
                    if is_resources
                    else F05_PAYMENT_BASELINE_LIVENESS
                )
                actual = (
                    matches[0].get("resources", {})
                    if is_resources
                    else matches[0].get("livenessProbe")
                )
                return actual == expected, _aware(self.clock()), (
                    f"kubernetes:{namespace}:{deployment}:{container}:{query.query_id}"
                )
            result = self._kubectl(
                "get", "pods", "--namespace", namespace,
                "--selector", f"app={deployment}", "-o", "json",
            )
            items = json.loads(result.stdout).get("items", [])
            active = [item for item in items if item.get("metadata", {}).get("deletionTimestamp") is None]
            if not active:
                raise LiveProbeError("payment pod is unavailable")
            statuses = [
                status
                for item in active
                for status in item.get("status", {}).get("containerStatuses", [])
                if status.get("name") == container
            ]
            if not statuses:
                raise LiveProbeError("payment container status is unavailable")
            if query.query_id == "kubernetes.container_restart_count":
                values = [status.get("restartCount") for status in statuses]
                if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
                    raise LiveProbeError("payment restart count is invalid")
                return sum(values), _aware(self.clock()), "kubernetes:payment:restart-count"
            reasons = [
                status.get("lastState", {}).get("terminated", {}).get("reason")
                for status in statuses
            ]
            reasons = [reason for reason in reasons if isinstance(reason, str) and reason]
            if query.query_id == "kubernetes.container_last_termination_reason":
                reason = "OOMKilled" if "OOMKilled" in reasons else (reasons[-1] if reasons else "None")
                return reason, _aware(self.clock()), "kubernetes:payment:last-termination-reason"
            if query.query_id == "kubernetes.container_oom_killed":
                return "OOMKilled" in reasons, _aware(self.clock()), "kubernetes:payment:oom-killed"
            if len(active) != 1:
                raise LiveProbeError("payment cgroup probe requires exactly one active pod")
            pod = active[0].get("metadata", {}).get("name")
            if not isinstance(pod, str) or not pod:
                raise LiveProbeError("payment pod name is invalid")
            result = self._kubectl(
                "exec", pod, "--namespace", namespace, "-c", container, "--",
                "cat", "/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max",
            )
            values = result.stdout.splitlines()
            if len(values) != 2 or not all(re.fullmatch(r"[0-9]+", value) for value in values):
                raise LiveProbeError("payment cgroup memory values are invalid")
            current, limit = map(int, values)
            value = (
                current
                if query.query_id == "kubernetes.container_memory_current_bytes"
                else limit
            )
            return value, _aware(self.clock()), f"kubernetes:payment:{query.query_id}"
        if query.query_id == "kubernetes.node_ready":
            if set(query.parameters) != {"node"}:
                raise LiveProbeError("node readiness query requires the fixed node parameter")
            node = str(query.parameters["node"])
            if node not in APPROVED_NODE_TARGETS:
                raise LiveProbeError("node is not allowlisted")
            result = self._kubectl("get", "node", node, "-o", "json")
            document = json.loads(result.stdout)
            ready = any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in document.get("status", {}).get("conditions", [])
            )
            return ready, _aware(self.clock()), f"kubernetes:node:{node}:ready"
        if query.query_id == "kubernetes.deployment_container_memory_limit":
            if dict(query.parameters) not in (
                F05_PAYMENT_TARGET, F15_FOOD_PAYMENT_TARGET, F20_FOOD_ORDER_TARGET,
            ):
                raise LiveProbeError("container memory limit target is not allowlisted")
            namespace = str(query.parameters["namespace"])
            deployment = str(query.parameters["deployment"])
            container = str(query.parameters["container"])
            result = self._kubectl(
                "get", "deployment", deployment, "--namespace", namespace, "-o", "json",
            )
            document = json.loads(result.stdout)
            containers = document.get("spec", {}).get("template", {}).get("spec", {}).get(
                "containers", []
            )
            matches = [item for item in containers if item.get("name") == container]
            if len(matches) != 1:
                raise LiveProbeError("deployment container is missing or ambiguous")
            value = matches[0].get("resources", {}).get("limits", {}).get("memory")
            if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]*(Mi|Gi)", value) is None:
                raise LiveProbeError("container memory limit is not a canonical value")
            return value, _aware(self.clock()), (
                f"kubernetes:{namespace}:{deployment}:{container}:memory-limit"
            )
        if query.query_id == "kubernetes.deployment_container_cpu_limit":
            if dict(query.parameters) != F12_PRODUCT_TARGET:
                raise LiveProbeError("container CPU limit target is not allowlisted")
            namespace = str(query.parameters["namespace"])
            deployment = str(query.parameters["deployment"])
            container = str(query.parameters["container"])
            result = self._kubectl(
                "get", "deployment", deployment, "--namespace", namespace, "-o", "json",
            )
            document = json.loads(result.stdout)
            containers = document.get("spec", {}).get("template", {}).get("spec", {}).get(
                "containers", []
            )
            matches = [item for item in containers if item.get("name") == container]
            if len(matches) != 1:
                raise LiveProbeError("deployment container is missing or ambiguous")
            value = matches[0].get("resources", {}).get("limits", {}).get("cpu")
            if not isinstance(value, str) or re.fullmatch(r"[1-9][0-9]*m", value) is None:
                raise LiveProbeError("container CPU limit is not a canonical millicpu value")
            return value, _aware(self.clock()), (
                f"kubernetes:{namespace}:{deployment}:{container}:cpu-limit"
            )
        if query.query_id == "kubernetes.deployment_available_replicas":
            target = (
                str(query.parameters.get("namespace")),
                str(query.parameters.get("deployment")),
            )
            if set(query.parameters) != {"namespace", "deployment"} or target not in APPROVED_DEPLOYMENT_REPLICA_TARGETS:
                raise LiveProbeError("deployment replica target is not allowlisted")
            namespace = str(query.parameters["namespace"])
            deployment = str(query.parameters["deployment"])
            result = self._kubectl(
                "get", "deployment", deployment, "--namespace", namespace, "-o", "json",
            )
            document = json.loads(result.stdout)
            value = document.get("status", {}).get("availableReplicas", 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise LiveProbeError("deployment available replicas is invalid")
            return value, _aware(self.clock()), (
                f"kubernetes:{namespace}:{deployment}:available-replicas"
            )
        if query.query_id == "kubernetes.kafka_consumer_lag":
            if dict(query.parameters) not in APPROVED_KAFKA_LAG_CONTRACTS:
                raise LiveProbeError("Kafka lag target is not allowlisted")
            namespace = str(query.parameters["namespace"])
            pod = str(query.parameters["pod"])
            bootstrap = str(query.parameters["bootstrap_server"])
            group = str(query.parameters["consumer_group"])
            result = self._kubectl(
                "exec", pod, "--namespace", namespace, "--",
                "/opt/kafka/bin/kafka-consumer-groups.sh",
                "--bootstrap-server", bootstrap, "--describe", "--group", group,
            )
            lag = 0
            rows = 0
            for line in result.stdout.splitlines():
                fields = line.split()
                if len(fields) < 6 or fields[0] != group:
                    continue
                try:
                    current = int(fields[5])
                except ValueError as error:
                    raise LiveProbeError("Kafka lag row is invalid") from error
                if current < 0:
                    raise LiveProbeError("Kafka lag cannot be negative")
                lag += current
                rows += 1
            if rows == 0:
                raise LiveProbeError("Kafka consumer group returned no partition rows")
            return lag, _aware(self.clock()), f"kubernetes:{namespace}:{pod}:{group}:lag"
        if query.query_id not in {
            "kubernetes.pod_ready", "kubernetes.image_pull_failure"
        }:
            raise LiveProbeError("kubernetes selector parameters are not approved")
        namespace = query.parameters.get("namespace", "rca-testbed-commerce")
        resource = query.parameters.get("resource", "api-gateway")
        selector = APPROVED_K8S_TARGETS.get((str(namespace), str(resource)))
        allowed = (
            {"namespace", "resource", "container"}
            if query.query_id == "kubernetes.image_pull_failure"
            else {"namespace", "resource"}
        )
        if selector is None or set(query.parameters) - allowed:
            raise LiveProbeError("kubernetes selector parameters are not approved")
        result = self._kubectl(
            "get", "pods", "--namespace", str(namespace),
            "--selector", selector, "-o", "json",
        )
        items = json.loads(result.stdout)["items"]
        if query.query_id == "kubernetes.image_pull_failure":
            container = query.parameters.get("container")
            if container != "payment-service" or resource != "testbed-payment":
                raise LiveProbeError("image-pull target is not allowlisted")
            failed = any(
                status.get("name") == container
                and status.get("state", {}).get("waiting", {}).get("reason")
                in {"ErrImagePull", "ImagePullBackOff", "ErrImageNeverPull"}
                for item in items
                for status in item.get("status", {}).get("containerStatuses", [])
            )
            return failed, _aware(self.clock()), (
                f"kubernetes:{namespace}:{resource}:{container}:image-pull-failure"
            )
        ready = bool(items) and all(
            any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in item.get("status", {}).get("conditions", [])
            )
            for item in items
        )
        return ready, _aware(self.clock()), f"kubernetes:{namespace}:{resource}:ready"

    def _database_observation(self, query: ApprovedQuery) -> tuple[Any, datetime, str]:
        if query.query_id == "database.oracle_tagged_session_count":
            if set(query.parameters) != {"client_identifier"}:
                raise LiveProbeError("Oracle session probe requires client_identifier")
            tag = query.parameters["client_identifier"]
            if tag not in APPROVED_ORACLE_TAGS:
                raise LiveProbeError("Oracle session tag is not allowlisted")
            result = self._kubectl(
                "exec", "testbed-oracle-0", "--namespace", "rca-testbed-banking", "--",
                "sh", "-lc",
                _oracle_sqlplus(
                    "select count(*) from v$session where client_identifier="
                    f"{_oracle_string_literal(tag)};"
                ),
            )
            raw = result.stdout.strip()
            if not re.fullmatch(r"[0-9]+", raw):
                raise LiveProbeError("Oracle tagged session count is invalid")
            return int(raw), _aware(self.clock()), "database:oracle-tagged-session-count"
        if query.query_id == "database.mysql_index_present":
            if dict(query.parameters) != MYSQL_INDEX_CONTRACT:
                raise LiveProbeError("MySQL index target is not allowlisted")
            result = self._kubectl(
                "exec", "testbed-mysql-0", "--namespace", "rca-testbed-food", "--",
                "sh", "-lc",
                "mysql -N -uroot -p\"$MYSQL_ROOT_PASSWORD\" fooddelivery -e \"SELECT count(*) FROM information_schema.statistics WHERE table_schema='fooddelivery' AND table_name='menus' AND index_name='idx_menus_category';\" 2>/dev/null",
            )
            raw = result.stdout.strip()
            if raw not in {"0", "1"}:
                raise LiveProbeError("MySQL index presence is invalid")
            return raw == "1", _aware(self.clock()), "database:mysql-index-present"
        if query.query_id == "database.outbox_unpublished_count":
            # F18-P decisive evidence: BANKING.outbox_events rows the halted
            # relay has not published (published_at IS NULL, init.sql:60-70).
            if dict(query.parameters) != OUTBOX_UNPUBLISHED_CONTRACT:
                raise LiveProbeError("outbox count target is not allowlisted")
            result = self._kubectl(
                "exec", "testbed-oracle-0", "--namespace", "rca-testbed-banking", "--",
                "sh", "-lc",
                _oracle_sqlplus(
                    "select count(*) from banking.outbox_events where published_at is null;"
                ),
            )
            raw = result.stdout.strip()
            if not re.fullmatch(r"[0-9]+", raw):
                raise LiveProbeError("outbox unpublished count is invalid")
            return int(raw), _aware(self.clock()), "database:outbox-unpublished-count"
        if query.query_id == "database.commerce_outbox_unpublished_count":
            if dict(query.parameters) != COMMERCE_OUTBOX_UNPUBLISHED_CONTRACT:
                raise LiveProbeError("commerce outbox count target is not allowlisted")
            response = self.database_client(
                COMMERCE_OUTBOX_UNPUBLISHED_SQL, (), credentials=self.database_credentials,
            )
            count = response.get("unpublished_count")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise LiveProbeError("commerce outbox unpublished count is invalid")
            return (
                count,
                _response_time(response, self.clock),
                "database:commerce-outbox-unpublished-count",
            )
        if query.query_id == "database.payment_duplicate_order_count_since_t1":
            state = self._f06_pulse_state(query)
            started_at = _parse_time(state.get("started_at"))
            response = self.database_client(
                PAYMENT_DUPLICATE_SINCE_T1_SQL,
                (_format_utc(started_at),),
                credentials=self.database_credentials,
            )
            count = response.get("duplicate_count")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise LiveProbeError("payment duplicate count is invalid")
            return (
                count,
                _response_time(response, self.clock),
                "database:payment-duplicate-order-count-since-t1",
            )
        if query.query_id == "database.pg_slow_active_query_count":
            if dict(query.parameters) != PG_SLOW_ACTIVE_QUERY_CONTRACT:
                raise LiveProbeError("PG slow-query target is not allowlisted")
            response = self.database_client(
                PG_SLOW_ACTIVE_QUERY_SQL, (), credentials=self.database_credentials,
            )
            count = response.get("slow_active_count")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise LiveProbeError("PG slow active query count is invalid")
            return count, _response_time(response, self.clock), "database:pg-slow-active-query-count"
        if query.query_id == "database.index_present":
            if dict(query.parameters) != INDEX_PRESENT_CONTRACT:
                raise LiveProbeError("database index target is not allowlisted")
            response = self.database_client(
                INDEX_PRESENT_SQL,
                (
                    str(query.parameters["schema"]),
                    str(query.parameters["table"]),
                    str(query.parameters["index"]),
                ),
                credentials=self.database_credentials,
            )
            present = response.get("index_present")
            if not isinstance(present, bool):
                raise LiveProbeError("database index result is invalid")
            return present, _response_time(response, self.clock), "database:index-present"
        if query.query_id == "database.inventory_stock_level":
            if dict(query.parameters) != INVENTORY_STOCK_CONTRACT:
                raise LiveProbeError("inventory stock target is not allowlisted")
            response = self.database_client(
                INVENTORY_ZERO_STOCK_SQL, (), credentials=self.database_credentials,
            )
            count = response.get("zero_stock_count")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise LiveProbeError("inventory zero-stock count is invalid")
            return count, _response_time(response, self.clock), "database:inventory-zero-stock-count"
        if query.query_id == "database.restock_movement_rate":
            if dict(query.parameters) != RESTOCK_MOVEMENT_CONTRACT:
                raise LiveProbeError("restock movement target is not allowlisted")
            response = self.database_client(
                RESTOCK_MOVEMENT_SQL, (), credentials=self.database_credentials,
            )
            count = response.get("restock_count")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise LiveProbeError("restock movement count is invalid")
            return count, _response_time(response, self.clock), "database:restock-movement-count"
        if query.query_id == "database.integrity_violation_count":
            # F17-P decisive evidence: transfers that completed against a
            # FROZEN/CLOSED account since the run's t1 — the dual-arm bypass
            # signature. 'since' is bound by the caller to the run start time.
            since = query.parameters.get("since")
            if set(query.parameters) != {"since"} or not isinstance(since, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", since
            ):
                raise LiveProbeError("integrity violation query requires a valid 'since' timestamp")
            result = self._kubectl(
                "exec", "testbed-oracle-0", "--namespace", "rca-testbed-banking", "--",
                "sh", "-lc",
                _oracle_sqlplus(
                    "select count(*) from banking.transfers t join banking.accounts a "
                    "on a.id in (t.from_account, t.to_account) "
                    f"where a.status in ({_oracle_string_literal('FROZEN')},"
                    f"{_oracle_string_literal('CLOSED')}) "
                    f"and t.status={_oracle_string_literal('COMPLETED')} "
                    f"and t.created_at >= to_timestamp({_oracle_string_literal(since)}, "
                    f"{_oracle_string_literal('YYYY-MM-DD HH24:MI:SS')});"
                ),
            )
            raw = result.stdout.strip()
            if not re.fullmatch(r"[0-9]+", raw):
                raise LiveProbeError("integrity violation count is invalid")
            return int(raw), _aware(self.clock()), "database:integrity-violation-count"
        if query.query_id == "database.ledger_unmatched_transfer_count":
            # F14-P decisive evidence: transfers that COMMITTED but whose ledger
            # rows never landed, because the consumer swallowed the write error
            # and still committed the offset.
            #
            # Deliberately NOT the double-entry imbalance.  recordTransfer writes
            # DEBIT and CREDIT in one transaction, so this defect drops both and
            # (DEBIT - CREDIT) stays exactly 0 forever -- the reconciliation batch
            # is structurally blind to it.  Asserting on imbalance would have been
            # a success condition that can never fire.
            #
            # Windowed rather than since-t1 so the signal also falls back to 0
            # once writes resume, which is what the recovery gate needs.  The
            # grace tail excludes transfers whose ledger write is still in flight.
            parameters = dict(query.parameters)
            if parameters != LEDGER_UNMATCHED_CONTRACT:
                raise LiveProbeError("ledger reconciliation window is not allowlisted")
            window = int(parameters["window_minutes"])
            grace = int(parameters["grace_seconds"])
            result = self._kubectl(
                "exec", "testbed-oracle-0", "--namespace", "rca-testbed-banking", "--",
                "sh", "-lc",
                _oracle_sqlplus(
                    "select count(*) from banking.transfers t "
                    f"where t.status={_oracle_string_literal('COMPLETED')} "
                    # sys_extract_utc: created_at is stored UTC-naive, so comparing it
                    # against a plain systimestamp would shift the window by the DB
                    # host's offset.
                    f"and t.created_at >= sys_extract_utc(systimestamp) - "
                    f"numtodsinterval({window}, {_oracle_string_literal('MINUTE')}) "
                    f"and t.created_at < sys_extract_utc(systimestamp) - "
                    f"numtodsinterval({grace}, {_oracle_string_literal('SECOND')}) "
                    "and not exists (select 1 from banking.ledger_entries le "
                    "where le.transfer_ref = t.transfer_ref);"
                ),
            )
            raw = result.stdout.strip()
            if not re.fullmatch(r"[0-9]+", raw):
                raise LiveProbeError("ledger unmatched transfer count is invalid")
            return int(raw), _aware(self.clock()), "database:ledger-unmatched-transfer-count"
        if query.query_id == "business.order_duplicate_count_since_t1":
            # F15-R non-idempotency guard: a 429-retry that creates a second
            # payment row for one order is the duplicate signature that would
            # reclassify the case as F14-R. t1 anchors on the flap worker start.
            state = self._f15r_flap_state(query)
            started_at = _parse_time(state.get("started_at"))
            response = self.database_client(
                PAYMENT_DUPLICATE_SINCE_T1_SQL,
                (_format_utc(started_at),),
                credentials=self.database_credentials,
            )
            count = response.get("duplicate_count")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise LiveProbeError("order duplicate count is invalid")
            return (
                count,
                _response_time(response, self.clock),
                "database:order-duplicate-count-since-t1",
            )
        if query.query_id == "database.blocked_session_count":
            if set(query.parameters) != {"schema", "table"}:
                raise LiveProbeError("blocked session probe requires schema and table")
            relation = f"{query.parameters['schema']}.{query.parameters['table']}"
            if relation not in BLOCKED_SESSION_RELATIONS:
                raise LiveProbeError("blocked session relation is not allowlisted")
            response = self.database_client(
                BLOCKED_SESSION_SQL,
                (relation,),
                credentials=self.database_credentials,
            )
            count = response.get("blocked_count")
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise LiveProbeError("blocked session count is invalid")
            return count, _response_time(response, self.clock), "database:blocked-session-count"
        if query.query_id != "database.tagged_session_count" or set(query.parameters) - {"scenario_tag"}:
            raise LiveProbeError("unsupported database query")
        tag = query.parameters.get("scenario_tag") or (
            f"lucida:{self.run_id}" if self.run_id is not None else None
        )
        if (
            not isinstance(tag, str)
            or len(tag) > 96
            or not (tag.startswith("lucida:") or tag in APPROVED_SESSION_TAGS)
        ):
            raise LiveProbeError("database scenario tag is invalid")
        response = self.database_client(
            TAGGED_SESSION_SQL,
            (tag,),
            credentials=self.database_credentials,
        )
        count = response["tagged_count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise LiveProbeError("database count is invalid")
        return count, _response_time(response, self.clock), "database:tagged-session-count"

    def _host_observation(self, query: ApprovedQuery) -> tuple[Any, datetime, str]:
        if set(query.parameters) != {"scenario_id"}:
            raise LiveProbeError("host probe requires an exact scenario id")
        scenario_id = query.parameters["scenario_id"]
        if scenario_id not in HOST_PROBE_CONTRACTS:
            raise LiveProbeError("host probe scenario is not allowlisted")
        host, mode, target = HOST_PROBE_CONTRACTS[str(scenario_id)]
        result = self.process_runner(
            [
                "ssh", "-i", LOADGEN_KEY, "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10",
                f"nkia@{host}", "sudo", "bash", "-s", "--",
                str(scenario_id), mode, target,
            ],
            input=HOST_PROBE_SCRIPT,
            check=True,
            capture_output=True,
            text=False,
            env=_read_only_environment(),
            timeout=15,
        )
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LiveProbeError("host probe returned invalid JSON") from error
        if query.query_id == "host.scenario_active":
            value = payload.get("active")
        elif query.query_id == "host.scenario_clean":
            value = payload.get("clean")
        elif query.query_id == "host.filesystem_used_percent":
            if not target:
                raise LiveProbeError("host scenario has no filesystem target")
            value = payload.get("filesystem_used_percent")
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
                raise LiveProbeError("filesystem utilization is invalid")
        elif query.query_id == "host.disk_io_utilization":
            # The storage-saturation scenarios (F02-H, F10-H, F10-P) need to show
            # that the device is busy, not merely that the database is slow —
            # without it "storage saturation" is indistinguishable from a lock,
            # a slow query, or a starved pool. KCM exposes node CPU and memory
            # but no per-device I/O, so this is measured on the host directly.
            if not target:
                raise LiveProbeError("host scenario has no filesystem target")
            value = payload.get("disk_io_utilization")
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
                raise LiveProbeError("disk utilization is invalid")
        else:
            raise LiveProbeError("unsupported host query")
        if query.query_id not in (
            "host.filesystem_used_percent",
            "host.disk_io_utilization",
        ) and not isinstance(value, bool):
            raise LiveProbeError("host state is invalid")
        return value, _aware(self.clock()), f"host:{scenario_id}:{query.query_id}"

    def _business_observation(self, query: ApprovedQuery) -> tuple[Any, datetime, str]:
        if query.query_id.startswith("mock."):
            state = self._f06_pulse_state(query)
            observed_at = _parse_time(state.get("observed_at"))
            field = {
                "mock.transient_consumed_count": "transient_consumed_count",
                "mock.duplicate_expectation_count": "duplicate_expectation_count",
                "mock.expired_unconsumed_count": "expired_unconsumed_count",
                "mock.transient_expectation_absent": "transient_expectation_absent",
                "mock.snapshot_restored": "snapshot_restored",
            }.get(query.query_id)
            if query.query_id == "mock.pulse_age_seconds":
                reference = state.get("last_pulse_at") or state.get("started_at")
                value: Any = (_aware(self.clock()) - _parse_time(reference)).total_seconds()
                if value < 0:
                    raise LiveProbeError("mock pulse timestamp is from the future")
            elif field is not None:
                value = state.get(field)
                if field in {"transient_expectation_absent", "snapshot_restored"}:
                    if not isinstance(value, bool):
                        raise LiveProbeError("mock pulse boolean is invalid")
                elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise LiveProbeError("mock pulse count is invalid")
            else:
                raise LiveProbeError("unsupported mock pulse query")
            return value, observed_at, f"mock-pulse:{query.query_id}"
        if query.query_id in {"scenario.mock_flap_episode", "scenario.mock_flap_fault_active"}:
            state = self._f15r_flap_state(query)
            observed_at = _parse_time(state.get("observed_at"))
            if query.query_id == "scenario.mock_flap_episode":
                value: Any = state.get("episode")
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise LiveProbeError("mock flap episode is invalid")
            else:
                value = state.get("fault_active")
                if not isinstance(value, bool):
                    raise LiveProbeError("mock flap fault state is invalid")
            return value, observed_at, f"mock-flap:{query.query_id}"
        if query.query_id != "business.checkout_invariant" or set(query.parameters) - {
            "business_key", "domain"
        }:
            raise LiveProbeError("unsupported business query")
        key = query.parameters.get("business_key", "checkout")
        if key not in APPROVED_BUSINESS_KEYS:
            raise LiveProbeError("business key is not allowlisted")
        # Same split the loadgen observations already had: without a domain this
        # reads the scenario's own k6 output, which exists only while that scenario
        # runs. business_ok is published in the resident baseline document too, and
        # this probe was the one instrument left out of the 07-29 change — so F08-H
        # and F11-R were the only success gates a no-fault sweep could not read.
        domain = query.parameters.get("domain")
        document = (
            self._baseline_live_document(domain) if domain else self._loadgen_live_document()
        )
        business_ok = document.get("business_ok")
        if not isinstance(business_ok, bool):
            raise LiveProbeError("checkout business outcome is unavailable")
        source = (
            f"k6:baseline:{domain}:business_ok" if domain else "k6:checkout-business-outcome"
        )
        return business_ok, _parse_time(document["observed_at"]), source

    def _f15r_flap_state(self, query: ApprovedQuery) -> dict[str, Any]:
        if dict(query.parameters) != {"scenario_id": "F15-R"}:
            raise LiveProbeError("mock flap scenario is not allowlisted")
        path = self.paths.profile_state / "F15-R-mock-flap-state.json"
        if path.parent != self.paths.profile_state:
            raise LiveProbeError("mock flap state path escaped the trusted root")
        document = _read_json(path)
        if document.get("scenario_id") != "F15-R":
            raise LiveProbeError("mock flap state belongs to another scenario")
        return document

    def _f06_pulse_state(self, query: ApprovedQuery) -> dict[str, Any]:
        if dict(query.parameters) != {"scenario_id": "F06-G"}:
            raise LiveProbeError("mock pulse scenario is not allowlisted")
        path = self.paths.profile_state / "F06-G-mock-pulse-state.json"
        if path.parent != self.paths.profile_state:
            raise LiveProbeError("mock pulse state path escaped the trusted root")
        document = _read_json(path)
        if document.get("scenario_id") != "F06-G":
            raise LiveProbeError("mock pulse state belongs to another scenario")
        return document

    def _baseline_live_document(self, domain: str) -> dict[str, Any]:
        """Read a domain's resident baseline live document.

        Separates the observation plane from the injection plane: this document
        exists whether or not the running scenario pours load into that domain,
        because the resident loadgen unit publishes it continuously.
        """
        if domain not in APPROVED_LOADGEN_DOMAINS:
            raise LiveProbeError("loadgen domain is not allowlisted")
        local = self.paths.baseline_summary_dir / f"baseline-{domain}-live.json"
        if local.parent != self.paths.baseline_summary_dir:
            raise LiveProbeError("baseline summary path escaped the trusted root")
        if local.is_file():
            document = _read_json(local)
        else:
            completed = self.process_runner(
                [
                    "ssh", "-i", LOADGEN_KEY,
                    "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=yes",
                    "-o", "ConnectTimeout=10",
                    f"{LOADGEN_USER}@{LOADGEN_HOST}",
                    "cat", "--", f"/tmp/rca-baseline-{domain}-live.json",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=_read_only_environment(),
                timeout=15,
            )
            document = json.loads(completed.stdout)
        if not isinstance(document, dict):
            raise LiveProbeError("baseline live observation is not an object")
        if document.get("domain") != domain:
            raise LiveProbeError("baseline live observation belongs to another domain")
        # A baseline document must never carry a scenario identity — if it does,
        # a scenario's own k6 output has been mistaken for the resident baseline.
        if "scenario_id" in document:
            raise LiveProbeError("baseline live observation claims a scenario identity")
        observed_at = _parse_time(document.get("observed_at"))
        age = (_aware(self.clock()) - observed_at).total_seconds()
        if not -CLOCK_SKEW_TOLERANCE_SEC <= age <= 30:
            raise LiveProbeError("baseline live observation is stale or from the future")
        return document

    def _loadgen_live_document(self) -> dict[str, Any]:
        # T-suffixed ids (F15-T1..T4) are timeline compositions and were excluded
        # by the single-letter pattern, so F15-T1 could never read its own load
        # summary — achieved_rps failed the probe on every tick.
        if self.scenario_id is None or not re.fullmatch(
            r"F[0-9]{2}-(?:[A-Z]|T[1-4])", self.scenario_id
        ):
            raise LiveProbeError("loadgen observation requires an allowlisted scenario id")
        if self.paths.loadgen_summary.is_file():
            document = _read_json(self.paths.loadgen_summary)
        else:
            completed = self.process_runner(
                [
                    "ssh", "-i", LOADGEN_KEY,
                    "-o", "BatchMode=yes",
                    "-o", "StrictHostKeyChecking=yes",
                    "-o", "ConnectTimeout=10",
                    f"{LOADGEN_USER}@{LOADGEN_HOST}",
                    "cat", "--", f"/tmp/rca-scenario-{self.scenario_id}-live.json",
                ],
                check=True,
                capture_output=True,
                text=True,
                env=_read_only_environment(),
                timeout=15,
            )
            document = json.loads(completed.stdout)
            if not isinstance(document, dict):
                raise LiveProbeError("loadgen live observation is not an object")
        if (
            document.get("scenario_id") != self.scenario_id
            or document.get("scenario_tag") != f"scenario_id={self.scenario_id}"
        ):
            raise LiveProbeError("k6 live observation is not tagged to this scenario")
        observed_at = _parse_time(document.get("observed_at"))
        age = (_aware(self.clock()) - observed_at).total_seconds()
        if not -CLOCK_SKEW_TOLERANCE_SEC <= age <= 30:
            raise LiveProbeError("k6 live observation is stale or from the future")
        return document

    def _capture_observation(self, query: ApprovedQuery) -> tuple[bool, datetime, str]:
        if query.query_id != "capture.export_complete" or set(query.parameters) - {"run_id"}:
            raise LiveProbeError("unsupported capture query")
        run_id = query.parameters.get("run_id") or self.run_id
        if not isinstance(run_id, str) or not run_id or not run_id.replace("-", "").isalnum():
            raise LiveProbeError("capture run id is invalid")
        path = self.paths.capture_root / run_id / "capture-complete.json"
        if path.parent.parent != self.paths.capture_root:
            raise LiveProbeError("capture path escaped the trusted root")
        document = _read_json(path)
        return True, _parse_time(document["observed_at"]), "capture:controller-artifact"


class SnapshotProducer:
    """Atomically refresh legacy file inputs using live read-only producers."""

    def __init__(
        self,
        probes: LiveProbeSet,
        *,
        registry: ApprovedQueryRegistry,
        evidence_path: Path,
        observation_path: Path,
    ) -> None:
        self.probes = probes
        self.registry = registry
        self.evidence_path = evidence_path
        self.observation_path = observation_path

    def refresh(
        self,
        eligibility: EligibilityRequest,
        observation_requests: Sequence[Mapping[str, Any]],
    ) -> tuple[EligibilityEvidence, dict[str, Any]]:
        evidence = self.probes.inspect(eligibility)
        values: dict[str, Mapping[str, Any]] = {}
        for request in observation_requests:
            query = self.registry.bind(request)
            if query.query_id in values:
                raise ObservationContractError(f"duplicate query_id: {query.query_id}")
            values[query.query_id] = self.probes.observe(query)
        document = {"schema_version": 1, "queries": values}
        _atomic_json(self.evidence_path, evidence.model_dump(mode="json"))
        _atomic_json(self.observation_path, document)
        return evidence, document


# Written when a run is created or as it progresses; a real run carries at least
# one even if it crashed before recording a timeline.
RUN_DIRECTORY_MARKERS = (
    "plan.json",
    "lease.json",
    "capsule.json",
    "state.json",
    "result.json",
    "timeline.json",
    "cleanup.json",
)


def _is_run_directory(run_dir: Path) -> bool:
    return any((run_dir / marker).is_file() for marker in RUN_DIRECTORY_MARKERS)


def _run_intervals(run_dir: Path) -> list[tuple[datetime, datetime]]:
    intervals: list[tuple[datetime, datetime]] = []
    readable_no_effect = False
    cleanup_end: datetime | None = None
    cleanup_path = run_dir / "cleanup.json"
    if cleanup_path.is_file():
        try:
            cleanup = _read_json(cleanup_path)
            if cleanup.get("succeeded") is True and cleanup.get("effect_ended_at"):
                cleanup_end = _parse_time(cleanup["effect_ended_at"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            cleanup_end = None
    candidates = [run_dir / "result.json", run_dir / "state.json", run_dir / "timeline.json"]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            document = _read_json(path)
            t1 = document.get("t1")
            t2 = document.get("t2")
            if t1 and t2:
                intervals.append((_parse_time(t1), _parse_time(t2)))
            changes = document.get("level_changes", [])
            if not changes and isinstance(document.get("events"), list):
                changes = document["events"]
            if not changes and (
                "level_changes" in document
                or "events" in document
                or document.get("status") in {"pending", "blocked"}
            ):
                readable_no_effect = True
            for change in changes:
                started = change.get("applied_at") or change.get("started_at")
                ended = change.get("effect_ended_at") or change.get("ended_at")
                if started:
                    intervals.append(
                        (
                            _parse_time(started),
                            _parse_time(ended)
                            if ended
                            else cleanup_end or datetime.max.replace(tzinfo=timezone.utc),
                        )
                    )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            intervals.append((datetime.min.replace(tzinfo=timezone.utc), datetime.max.replace(tzinfo=timezone.utc)))
    if not intervals and not readable_no_effect:
        # An existing run directory without a readable timeline cannot prove a
        # clean window.  Treat it as an unknown open interval.
        intervals.append(
            (
                datetime.min.replace(tzinfo=timezone.utc),
                datetime.max.replace(tzinfo=timezone.utc),
            )
        )
    return intervals


def _intersects(left_start: datetime, left_end: datetime, right_start: datetime, right_end: datetime) -> bool:
    return left_start <= right_end and right_start <= left_end


def _read_json(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise LiveProbeError(f"expected a JSON object: {path}")
    return document


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise LiveProbeError("timestamp must be an ISO-8601 string")
    return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LiveProbeError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return _aware(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def _response_time(response: Mapping[str, Any], clock: Clock) -> datetime:
    raw = response.get("observed_at")
    return _parse_time(raw) if raw is not None else _aware(clock())


def _response_json(response: Mapping[str, Any]) -> Mapping[str, Any]:
    body = response.get("json")
    if not isinstance(body, Mapping):
        raise LiveProbeError("HTTP response has no JSON object")
    return body


def _read_only_environment() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"),
        "HOME": "/root",
        "KUBECONFIG": KUBECONFIG,
        "LC_ALL": "C",
    }
