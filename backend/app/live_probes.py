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
TARGET_HEALTH_URL = "http://192.168.122.77:30080/health"
PROMETHEUS_URL = os.environ.get(
    "PROMETHEUS_URL", "http://192.168.230.119:18428/api/v1/query"
)

APPROVED_CHECK_IDS = frozenset(
    {
        "kube-context",
        "kube-node-set",
        "coordinator-clean",
        "clean-window",
        "baseline-traffic",
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
    "kcm-pod-cpu-throttled-time-v1": (
        'max without(grade) (kcm.pod.cpu_throttled_time{namespace="rca-testbed-commerce",'
        'pod=~"testbed-product-.*"})'
    ),
    "kcm-workload-network-error-rate-v1": (
        'max without(grade) (kcm.pod.network_rx_error{namespace="rca-testbed-commerce",'
        'pod=~"testbed-product-.*"}) + max without(grade) '
        '(kcm.pod.network_tx_error{namespace="rca-testbed-commerce",'
        'pod=~"testbed-product-.*"})'
    ),
}
APPROVED_SERVICES = frozenset({"commerce-gateway", "commerce-order", "commerce-payment"})
APPROVED_APM_SERVICES = frozenset(
    {"commerce-gateway", "commerce-product", "commerce-order", "commerce-pricing"}
)
APPROVED_NODE_TARGETS = frozenset({"tb-w1", "tb-w2", "tb-w3"})
APPROVED_BUSINESS_KEYS = frozenset({"checkout", "order-1"})
APPROVED_K8S_TARGETS = {
    ("rca-testbed-commerce", "api-gateway"): "app=testbed-gateway",
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
}
F12_PRODUCT_TARGET = {
    "namespace": "rca-testbed-commerce",
    "deployment": "testbed-product",
    "container": "product-service",
}
ORACLE_TAG_CONTRACT = {"client_identifier": "rca-F01-P-oracle-lock"}
MYSQL_INDEX_CONTRACT = {
    "database": "fooddelivery", "table": "menus", "index": "idx_menus_category"
}
HOST_PROBE_CONTRACTS = {
    "F02-H": ("192.168.122.184", "fio", "/opt/local-path-provisioner/pvc-5d71e22a-1225-4505-a7cc-5cf29dad4cf5_rca-testbed-commerce_pgdata-testbed-postgres-0"),
    "F10-R": ("192.168.122.184", "watermark", "/opt/local-path-provisioner/pvc-5d71e22a-1225-4505-a7cc-5cf29dad4cf5_rca-testbed-commerce_pgdata-testbed-postgres-0"),
    "F10-H": ("192.168.122.14", "fio", "/opt/local-path-provisioner/pvc-3439d85f-f921-4b19-8808-c679506a31dd_rca-testbed-food_mysqldata-testbed-mysql-0"),
    "F10-P": ("192.168.122.184", "fio", "/opt/local-path-provisioner/pvc-01f9e717-727a-4227-a09b-584ec371c99f_rca-testbed-banking_oracledata-testbed-oracle-0"),
    "F15-P": ("192.168.122.11", "pressure", ""),
}
HOST_PROBE_SCRIPT = br'''#!/usr/bin/env bash
set -euo pipefail
scenario="$1"; mode="$2"; target="$3"
state_root=/var/lib/lucida/scenario-profile-state
pidfile="$state_root/${scenario}.pid"
active=false; artifact=false; used=0
if [[ -s "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then active=true; fi
case "$mode" in
 fio) [[ -e "$target/.lucida-${scenario}-fio" ]] && artifact=true ;;
 watermark) [[ -e "$target/.lucida-${scenario}-watermark" ]] && artifact=true; $artifact && active=true ;;
 pressure) ;;
 *) exit 2 ;;
esac
if [[ -n "$target" ]]; then used=$(df -P "$target" | awk 'NR==2{gsub(/%/,"",$5); print $5}'); fi
clean=true; $active && clean=false; $artifact && clean=false
printf '{"active":%s,"clean":%s,"filesystem_used_percent":%s}\n' "$active" "$clean" "$used"
'''
TAGGED_SESSION_SQL = (
    "SELECT count(*) AS tagged_count FROM pg_stat_activity "
    "WHERE application_name = current_setting('lucida.scenario_tag')"
)
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
INDEX_PRESENT_CONTRACT = {
    "db_host": "192.168.122.77",
    "db_port": 30432,
    "db_name": "commerce",
    "db_user": "commerce",
    "schema": "product_schema",
    "table": "products",
    "index": "idx_products_name",
}
DEPLOYMENT_REPLICAS_CONTRACT = {
    "namespace": "rca-testbed-commerce",
    "deployment": "testbed-shipping",
}
APPROVED_DEPLOYMENT_REPLICA_TARGETS = frozenset(
    {
        ("rca-testbed-commerce", "testbed-shipping"),
        ("rca-testbed-commerce", "testbed-payment"),
        ("rca-testbed-commerce", "testbed-product"),
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
            "target-health": self._target_healthy,
        }
        results = {check: _safe_bool(checks[check]) for check in request.checks}
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
        if query.parameters or query.query_id not in {
            "loadgen.achieved_rps", "loadgen.checkout_5xx_rate"
        }:
            raise LiveProbeError("unsupported loadgen query")
        document = self._loadgen_live_document()
        field = {
            "loadgen.achieved_rps": "achieved_rps",
            "loadgen.checkout_5xx_rate": "checkout_5xx_rate",
        }[query.query_id]
        value = document.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise LiveProbeError(f"k6 {field} is invalid")
        if field == "checkout_5xx_rate" and value > 1:
            raise LiveProbeError("k6 checkout_5xx_rate is outside [0,1]")
        return float(value), _parse_time(document["observed_at"]), f"k6:{field}"

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
        elif query.query_id == "prometheus.container_cpu_throttled_time":
            if dict(query.parameters) != F12_PRODUCT_TARGET:
                raise LiveProbeError("CPU throttle target is not allowlisted")
            promql = PROMETHEUS_TEMPLATES[query.template_id]
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
                "--selector", "app=testbed-payment", "-o", "json",
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
            if dict(query.parameters) not in (F05_PAYMENT_TARGET, F15_FOOD_PAYMENT_TARGET):
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
            if dict(query.parameters) != KAFKA_LAG_CONTRACT:
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
            if dict(query.parameters) != ORACLE_TAG_CONTRACT:
                raise LiveProbeError("Oracle session tag is not allowlisted")
            result = self._kubectl(
                "exec", "testbed-oracle-0", "--namespace", "rca-testbed-banking", "--",
                "sh", "-lc",
                "printf 'alter session set container=FREEPDB1;\\nset pages 0 feedback off heading off\\nselect count(*) from v$session where client_identifier=chr(114)||chr(99)||chr(97)||chr(45)||chr(70)||chr(48)||chr(49)||chr(45)||chr(80)||chr(45)||chr(111)||chr(114)||chr(97)||chr(99)||chr(108)||chr(101)||chr(45)||chr(108)||chr(111)||chr(99)||chr(107);\\nexit;\\n' | sqlplus -s / as sysdba",
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
        if query.query_id != "database.tagged_session_count" or set(query.parameters) - {"scenario_tag"}:
            raise LiveProbeError("unsupported database query")
        tag = query.parameters.get("scenario_tag") or (
            f"lucida:{self.run_id}" if self.run_id is not None else None
        )
        if (
            not isinstance(tag, str)
            or len(tag) > 96
            or not (
                tag.startswith("lucida:")
                or tag in {
                    "rca-F01-R-inventory-lock",
                    "rca-F02-G-batch-heavy-sql",
                    "rca-F06-H-payment-lock",
                    "rca-F15-T1-inventory-lock",
                }
            )
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
        else:
            raise LiveProbeError("unsupported host query")
        if query.query_id != "host.filesystem_used_percent" and not isinstance(value, bool):
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
        if query.query_id != "business.checkout_invariant" or set(query.parameters) - {"business_key"}:
            raise LiveProbeError("unsupported business query")
        key = query.parameters.get("business_key", "checkout")
        if key not in APPROVED_BUSINESS_KEYS:
            raise LiveProbeError("business key is not allowlisted")
        document = self._loadgen_live_document()
        business_ok = document.get("business_ok")
        if not isinstance(business_ok, bool):
            raise LiveProbeError("checkout business outcome is unavailable")
        return business_ok, _parse_time(document["observed_at"]), "k6:checkout-business-outcome"

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

    def _loadgen_live_document(self) -> dict[str, Any]:
        if self.scenario_id is None or not re.fullmatch(r"F[0-9]{2}-[A-Z]", self.scenario_id):
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


def _safe_bool(action: Callable[[], bool]) -> bool:
    try:
        return action() is True
    except Exception:
        return False


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
