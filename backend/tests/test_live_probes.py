from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from app.adaptive_runtime import EligibilityRequest
from app.live_probes import (
    DEPLOYMENT_REPLICAS_CONTRACT,
    BLOCKED_SESSION_SQL,
    EXPECTED_KUBE_CONTEXT,
    EXPECTED_KUBE_NODES,
    F12_PRODUCT_TARGET,
    COMMERCE_OUTBOX_UNPUBLISHED_CONTRACT,
    COMMERCE_OUTBOX_UNPUBLISHED_SQL,
    INDEX_PRESENT_CONTRACT,
    INDEX_PRESENT_SQL,
    INVENTORY_STOCK_CONTRACT,
    INVENTORY_ZERO_STOCK_SQL,
    RESTOCK_MOVEMENT_CONTRACT,
    RESTOCK_MOVEMENT_SQL,
    KAFKA_LAG_CONTRACT,
    PAYMENT_DUPLICATE_SINCE_T1_SQL,
    KUBECONFIG,
    PROMETHEUS_URL,
    TAGGED_SESSION_SQL,
    TARGET_HEALTH_URL,
    LiveProbeSet,
    ProbePaths,
    SnapshotProducer,
)
from app.observations import (
    ApprovedQueryRegistry,
    HttpProbeAdapter,
    ObservationContractError,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


def _write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


class Fakes:
    def __init__(self) -> None:
        self.process_calls: list[tuple[list[str], dict]] = []
        self.http_calls: list[tuple[str, str, dict]] = []
        self.database_calls: list[tuple[str, tuple[str, ...], dict]] = []

    def process(self, argv, **kwargs):
        argv = list(argv)
        self.process_calls.append((argv, kwargs))
        if argv[-2:] == ["config", "current-context"]:
            return subprocess.CompletedProcess(argv, 0, EXPECTED_KUBE_CONTEXT + "\n", "")
        if argv[-4:] == ["get", "nodes", "-o", "json"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps({"items": [{"metadata": {"name": item}} for item in EXPECTED_KUBE_NODES]}),
                "",
            )
        if "pods" in argv:
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": "testbed-payment-abc"},
                                "status": {
                                    "conditions": [{"type": "Ready", "status": "True"}],
                                    "containerStatuses": [
                                        {
                                            "name": "payment-service",
                                            "restartCount": 2,
                                            "lastState": {"terminated": {"reason": "Error"}},
                                            "state": {"waiting": {"reason": "ImagePullBackOff"}},
                                        }
                                    ],
                                }
                            }
                        ]
                    }
                ),
                "",
            )
        if "deployment" in argv and "testbed-shipping" in argv:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"status": {"availableReplicas": 0}}), ""
            )
        if "deployment" in argv and "testbed-product" in argv:
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    {
                        "spec": {
                            "template": {
                                "spec": {
                                    "containers": [
                                        {
                                            "name": "product-service",
                                            "resources": {"limits": {"cpu": "500m"}},
                                        }
                                    ]
                                }
                            }
                        }
                    }
                ),
                "",
            )
        if "deployment" in argv and "testbed-payment" in argv:
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps(
                    {
                        "status": {"availableReplicas": 1},
                        "spec": {
                            "template": {
                                "spec": {
                                    "containers": [
                                        {
                                            "name": "payment-service",
                                            "resources": {
                                                "limits": {"cpu": "500m", "memory": "1Gi"},
                                                "requests": {"cpu": "200m", "memory": "512Mi"},
                                            },
                                            "livenessProbe": {
                                                "failureThreshold": 5,
                                                "httpGet": {
                                                    "path": "/actuator/health",
                                                    "port": 8083,
                                                    "scheme": "HTTP",
                                                },
                                                "periodSeconds": 15,
                                                "successThreshold": 1,
                                                "timeoutSeconds": 3,
                                            },
                                        }
                                    ]
                                }
                            }
                        },
                    }
                ),
                "",
            )
        if "/sys/fs/cgroup/memory.current" in argv:
            return subprocess.CompletedProcess(argv, 0, "574550016\n1073741824\n", "")
        if any(item.endswith("/kafka-consumer-groups.sh") for item in argv):
            return subprocess.CompletedProcess(
                argv,
                0,
                "GROUP TOPIC PARTITION CURRENT-OFFSET LOG-END-OFFSET LAG CONSUMER-ID\n"
                "shipping-service shipping-events 0 10 14 4 consumer-1\n"
                "shipping-service shipping-events 1 20 23 3 consumer-2\n",
                "",
            )
        if "testbed-oracle-0" in argv and any("banking.transfers" in item for item in argv):
            return subprocess.CompletedProcess(argv, 0, "2\n", "")
        if argv[0] == "ssh":
            if argv[-2:] == ["--", "/tmp/rca-scenario-F07-H-live.json"]:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps(
                        {
                            "scenario_id": "F07-H",
                            "scenario_tag": "scenario_id=F07-H",
                            "achieved_rps": 71.0,
                            "checkout_5xx_rate": 0.25,
                            "entry_status": 429,
                            "business_ok": False,
                            "observed_at": NOW.isoformat(),
                        }
                    ),
                    "",
                )
            return subprocess.CompletedProcess(argv, 0, "active\n", "")
        raise AssertionError(argv)

    def http(self, method, url, **kwargs):
        self.http_calls.append((method, url, kwargs))
        if url == TARGET_HEALTH_URL:
            return {"status": 200, "observed_at": NOW.isoformat()}
        if url == PROMETHEUS_URL:
            return {
                "status": 200,
                "json": {"data": {"result": [{"value": [NOW.timestamp(), "0.321"]}]}},
            }
        raise AssertionError(url)

    def database(self, sql, parameters, **kwargs):
        self.database_calls.append((sql, parameters, kwargs))
        if sql == INDEX_PRESENT_SQL:
            return {"index_present": False, "observed_at": NOW.isoformat()}
        if sql == PAYMENT_DUPLICATE_SINCE_T1_SQL:
            return {"duplicate_count": 0, "observed_at": NOW.isoformat()}
        if sql == INVENTORY_ZERO_STOCK_SQL:
            return {"zero_stock_count": 2, "observed_at": NOW.isoformat()}
        if sql == RESTOCK_MOVEMENT_SQL:
            return {"restock_count": 0, "observed_at": NOW.isoformat()}
        if sql == COMMERCE_OUTBOX_UNPUBLISHED_SQL:
            return {"unpublished_count": 37, "observed_at": NOW.isoformat()}
        if sql == BLOCKED_SESSION_SQL:
            return {"blocked_count": 4, "observed_at": NOW.isoformat()}
        return {"tagged_count": 3, "observed_at": NOW.isoformat()}


def _paths(tmp_path: Path) -> ProbePaths:
    return ProbePaths(
        coordinator=tmp_path / "state" / "coordinator.json",
        runs=tmp_path / "runs",
        baseline_status=tmp_path / "state" / "baseline.json",
        loadgen_summary=tmp_path / "state" / "k6-summary.json",
        capture_root=tmp_path / "runs",
        profile_state=tmp_path / "profile-state",
    )


def _probes(tmp_path: Path, fakes: Fakes, *, credentials=None, scenario_id="F07-H") -> LiveProbeSet:
    paths = _paths(tmp_path)
    _write(
        paths.baseline_status,
        {"unit": "loadgen-commerce", "active": True, "observed_at": NOW.isoformat()},
    )
    _write(
        paths.loadgen_summary,
        {
            "scenario_id": "F07-H",
            "scenario_tag": "scenario_id=F07-H",
            "achieved_rps": 72.5,
            "checkout_5xx_rate": 0.125,
            "entry_status": 200,
            "business_ok": True,
            "observed_at": NOW.isoformat(),
        },
    )
    return LiveProbeSet(
        process_runner=fakes.process,
        http_client=fakes.http,
        database_client=fakes.database,
        database_credentials=credentials or {"password": "top-secret", "user": "probe"},
        scenario_id=scenario_id,
        clock=lambda: NOW,
        paths=paths,
    )


def _eligibility(*, checks=None) -> EligibilityRequest:
    return EligibilityRequest(
        run_id="new-run",
        scenario_id="F07-H",
        checks=checks
        or [
            "kube-context",
            "kube-node-set",
            "coordinator-clean",
            "clean-window",
            "baseline-traffic",
            "target-health",
        ],
        clean_window_sec=7200,
        requested_at=NOW,
    )


def test_eligibility_uses_canonical_kubeconfig_exact_context_nodes_and_fixed_health(tmp_path) -> None:
    fakes = Fakes()
    evidence = _probes(tmp_path, fakes).inspect(_eligibility())

    assert all(evidence.check_results.values())
    assert evidence.clean_window_start == NOW - timedelta(hours=2)
    assert evidence.clean_window_end == NOW
    assert evidence.baseline_active
    kubectl_calls = [call for call, _ in fakes.process_calls if call[0] == "kubectl"]
    assert kubectl_calls == [
        ["kubectl", "--kubeconfig", KUBECONFIG, "config", "current-context"],
        ["kubectl", "--kubeconfig", KUBECONFIG, "get", "nodes", "-o", "json"],
    ]
    assert [(method, url) for method, url, _ in fakes.http_calls] == [("GET", TARGET_HEALTH_URL)]


def test_unknown_check_is_rejected_before_any_external_call(tmp_path) -> None:
    fakes = Fakes()
    with pytest.raises(ObservationContractError, match="unknown approved check"):
        _probes(tmp_path, fakes).inspect(_eligibility(checks=["shell.anything"]))
    assert not fakes.process_calls
    assert not fakes.http_calls


def test_full_two_hour_window_blocks_timeline_overlap_and_dirty_coordinator(tmp_path) -> None:
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    paths = _paths(tmp_path)
    _write(
        paths.coordinator,
        {
            "next_fencing_token": 3,
            "active_lease": None,
            "dirty_run": {
                "run_id": "dirty-run",
                "scenario_id": "F01-R",
                "fencing_token": 2,
                "reason": "cleanup_failed",
                "marked_at": (NOW - timedelta(hours=5)).isoformat(),
            },
            "cleanup_claim": None,
        },
    )
    _write(
        paths.runs / "old-overlap" / "timeline.json",
        {
            "level_changes": [
                {
                    "applied_at": (NOW - timedelta(hours=2, minutes=1)).isoformat(),
                    "effect_ended_at": (NOW - timedelta(hours=1, minutes=59)).isoformat(),
                }
            ]
        },
    )

    evidence = probes.inspect(_eligibility(checks=["coordinator-clean", "clean-window", "baseline-traffic"]))
    assert evidence.check_results == {
        "coordinator-clean": False,
        "clean-window": False,
        "baseline-traffic": True,
    }
    assert evidence.overlapping_run_ids == ["dirty-run", "old-overlap"]


def test_current_fenced_run_is_not_counted_as_a_baseline_overlap(tmp_path) -> None:
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    paths = _paths(tmp_path)
    _write(
        paths.coordinator,
        {
            "next_fencing_token": 2,
            "active_lease": {
                "run_id": "new-run",
                "scenario_id": "F07-H",
                "fencing_token": 1,
                "acquired_at": (NOW - timedelta(seconds=1)).isoformat(),
                "heartbeat_at": NOW.isoformat(),
                "expires_at": (NOW + timedelta(seconds=30)).isoformat(),
            },
            "dirty_run": None,
            "cleanup_claim": None,
        },
    )
    evidence = probes.inspect(
        _eligibility(checks=["coordinator-clean", "clean-window", "baseline-traffic"])
    )
    assert evidence.check_results == {
        "coordinator-clean": True,
        "clean-window": True,
        "baseline-traffic": True,
    }
    assert evidence.overlapping_run_ids == []


def test_baseline_ssh_is_one_fixed_read_only_command_when_artifact_absent(tmp_path) -> None:
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    probes.paths.baseline_status.unlink()
    evidence = probes.inspect(_eligibility(checks=["baseline-traffic"]))

    assert evidence.baseline_active
    argv, kwargs = fakes.process_calls[0]
    assert argv[-1] == "systemctl is-active loadgen-commerce"
    assert argv[0] == "ssh"
    assert "StrictHostKeyChecking=yes" in argv
    assert kwargs["env"]["KUBECONFIG"] == KUBECONFIG


def test_all_producers_use_fixed_templates_selectors_and_parameterized_database(tmp_path) -> None:
    fakes = Fakes()
    secret = "top-secret-value"
    probes = _probes(tmp_path, fakes, credentials={"password": secret, "user": "probe"})
    paths = _paths(tmp_path)
    _write(paths.capture_root / "run-1" / "capture-complete.json", {"observed_at": NOW.isoformat()})
    registry = ApprovedQueryRegistry.from_path()
    requests = [
        {"query_id": "loadgen.achieved_rps"},
        {"query_id": "loadgen.checkout_5xx_rate"},
        {"query_id": "http.entry_health"},
        {"query_id": "prometheus.user_p95"},
        {
            "query_id": "kubernetes.pod_ready",
            "parameters": {"namespace": "rca-testbed-commerce", "resource": "api-gateway"},
        },
        {"query_id": "database.tagged_session_count", "parameters": {"scenario_tag": "lucida:run-1"}},
        {"query_id": "business.checkout_invariant", "parameters": {"business_key": "checkout"}},
        {"query_id": "capture.export_complete", "parameters": {"run_id": "run-1"}},
    ]
    values = {request["query_id"]: probes.observe(registry.bind(request)) for request in requests}

    assert all(item["quality"] == "good" for item in values.values())
    assert values["loadgen.achieved_rps"]["value"] == 72.5
    assert values["loadgen.checkout_5xx_rate"]["value"] == 0.125
    prom_call = next(item for item in fakes.http_calls if item[1] == PROMETHEUS_URL)
    rendered = parse_qs(prom_call[2]["body"].decode())["query"][0]
    assert rendered == (
        'histogram_quantile(0.95, sum by (le) '
        '(rate(http_server_request_duration_seconds_bucket{service_name=~"commerce-gateway"}[2m])))'
    )
    kube_argv = next(argv for argv, _ in fakes.process_calls if "pods" in argv)
    assert kube_argv[-8:] == [
        "get", "pods", "--namespace", "rca-testbed-commerce", "--selector", "app=testbed-gateway", "-o", "json"
    ]
    sql, parameters, kwargs = fakes.database_calls[0]
    assert sql == TAGGED_SESSION_SQL and parameters == ("lucida:run-1",)
    all_argv = json.dumps([argv for argv, _ in fakes.process_calls])
    assert secret not in all_argv and kwargs["credentials"]["password"] == secret


def test_f01r_database_observation_counts_blocked_sessions_on_the_target_relation(tmp_path) -> None:
    # F01-R/F06-H no longer tag the injecting session (the tag was the answer in
    # plain text), so the controller confirms the lock by its victims instead.
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    query = ApprovedQueryRegistry.from_path().bind(
        {
            "query_id": "database.blocked_session_count",
            "parameters": {"schema": "inventory_schema", "table": "inventory"},
        }
    )
    value = probes.observe(query)
    assert value["quality"] == "good" and value["value"] == 4
    sql, parameters, _ = fakes.database_calls[0]
    assert sql == BLOCKED_SESSION_SQL and parameters == ("inventory_schema.inventory",)


def test_timeline_scenario_ids_can_read_their_own_load_summary(tmp_path) -> None:
    # F15-T1..T4 are timeline compositions; the old single-letter id pattern
    # excluded them, so achieved_rps errored on every tick of F15-T1.
    fakes = Fakes()
    probes = _probes(tmp_path, fakes, scenario_id="F15-T1")
    _write(
        _paths(tmp_path).loadgen_summary,
        {
            "scenario_id": "F15-T1",
            "scenario_tag": "scenario_id=F15-T1",
            "achieved_rps": 72.5,
            "checkout_5xx_rate": 0.125,
            "entry_status": 200,
            "business_ok": True,
            "observed_at": NOW.isoformat(),
        },
    )
    query = ApprovedQueryRegistry.from_path().bind({"query_id": "loadgen.achieved_rps"})
    value = probes.observe(query)
    assert value["quality"] == "good" and value["value"] == 72.5


def test_blocked_session_probe_rejects_relations_outside_the_approved_lock_levels(tmp_path) -> None:
    probes = _probes(tmp_path, Fakes())
    query = ApprovedQueryRegistry.from_path().bind(
        {
            "query_id": "database.blocked_session_count",
            "parameters": {"schema": "public", "table": "orders"},
        }
    )
    value = probes.observe(query)
    assert value["quality"] == "error"


def test_f02r_index_probe_is_fixed_and_parameterized(tmp_path) -> None:
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    query = ApprovedQueryRegistry.from_path().bind(
        {"query_id": "database.index_present", "parameters": INDEX_PRESENT_CONTRACT}
    )

    observed = probes.observe(query)

    assert observed["quality"] == "good" and observed["value"] is False
    sql, parameters, _ = fakes.database_calls[0]
    assert sql == INDEX_PRESENT_SQL
    assert parameters == ("product_schema", "products", "idx_products_name")

    tampered = dict(INDEX_PRESENT_CONTRACT)
    tampered["index"] = "users_pkey"
    rejected = probes.observe(
        ApprovedQueryRegistry.from_path().bind(
            {"query_id": "database.index_present", "parameters": tampered}
        )
    )
    assert rejected["quality"] == "error" and rejected["value"] is None


def test_f23r_inventory_stock_and_restock_probes_are_fixed_and_parameterized(tmp_path) -> None:
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    registry = ApprovedQueryRegistry.from_path()

    stock = probes.observe(
        registry.bind({"query_id": "database.inventory_stock_level", "parameters": INVENTORY_STOCK_CONTRACT})
    )
    assert stock["quality"] == "good" and stock["value"] == 2
    sql, parameters, _ = fakes.database_calls[0]
    assert sql == INVENTORY_ZERO_STOCK_SQL and parameters == ()

    restock = probes.observe(
        registry.bind({"query_id": "database.restock_movement_rate", "parameters": RESTOCK_MOVEMENT_CONTRACT})
    )
    assert restock["quality"] == "good" and restock["value"] == 0
    sql, parameters, _ = fakes.database_calls[1]
    assert sql == RESTOCK_MOVEMENT_SQL and parameters == ()

    tampered = dict(INVENTORY_STOCK_CONTRACT)
    tampered["table"] = "products"
    rejected = probes.observe(
        registry.bind({"query_id": "database.inventory_stock_level", "parameters": tampered})
    )
    assert rejected["quality"] == "error" and rejected["value"] is None


def test_f04h_commerce_outbox_probe_is_fixed_and_separate_from_banking(tmp_path) -> None:
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    registry = ApprovedQueryRegistry.from_path()

    observed = probes.observe(
        registry.bind(
            {
                "query_id": "database.commerce_outbox_unpublished_count",
                "parameters": COMMERCE_OUTBOX_UNPUBLISHED_CONTRACT,
            }
        )
    )

    assert observed["quality"] == "good" and observed["value"] == 37
    sql, parameters, _ = fakes.database_calls[0]
    assert sql == COMMERCE_OUTBOX_UNPUBLISHED_SQL and parameters == ()
    # The banking probe shells into Oracle; this one must never take that path.
    assert not [call for call, _ in fakes.process_calls if call[0] == "kubectl"]

    tampered = dict(COMMERCE_OUTBOX_UNPUBLISHED_CONTRACT)
    tampered["schema"] = "banking"
    rejected = probes.observe(
        registry.bind(
            {"query_id": "database.commerce_outbox_unpublished_count", "parameters": tampered}
        )
    )
    assert rejected["quality"] == "error" and rejected["value"] is None


def test_f17p_integrity_violation_probe_requires_a_valid_since_timestamp(tmp_path) -> None:
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    registry = ApprovedQueryRegistry.from_path()

    observed = probes.observe(
        registry.bind({"query_id": "database.integrity_violation_count", "parameters": {"since": "2026-07-24 09:00:00"}})
    )
    assert observed["quality"] == "good" and observed["value"] == 2
    exec_argv = fakes.process_calls[-1][0]
    assert "testbed-oracle-0" in exec_argv and "--namespace" in exec_argv and "rca-testbed-banking" in exec_argv

    rejected = probes.observe(
        registry.bind(
            {
                "query_id": "database.integrity_violation_count",
                "parameters": {"since": "2026-07-24T09:00:00Z; drop table transfers;"},
            }
        )
    )
    assert rejected["quality"] == "error" and rejected["value"] is None


def test_f04r_replica_and_kafka_lag_probes_use_exact_targets(tmp_path) -> None:
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    registry = ApprovedQueryRegistry.from_path()

    replicas = probes.observe(
        registry.bind(
            {
                "query_id": "kubernetes.deployment_available_replicas",
                "parameters": DEPLOYMENT_REPLICAS_CONTRACT,
            }
        )
    )
    lag = probes.observe(
        registry.bind(
            {"query_id": "kubernetes.kafka_consumer_lag", "parameters": KAFKA_LAG_CONTRACT}
        )
    )

    assert replicas["quality"] == "good" and replicas["value"] == 0
    assert lag["quality"] == "good" and lag["value"] == 7
    replica_argv = next(argv for argv, _ in fakes.process_calls if "deployment" in argv)
    assert replica_argv[-7:] == [
        "get", "deployment", "testbed-shipping", "--namespace", "rca-testbed-commerce", "-o", "json"
    ]
    lag_argv = next(
        argv for argv, _ in fakes.process_calls
        if any(item.endswith("/kafka-consumer-groups.sh") for item in argv)
    )
    assert lag_argv[:8] == [
        "kubectl", "--kubeconfig", KUBECONFIG, "exec", "testbed-kafka-0",
        "--namespace", "rca-testbed-commerce", "--",
    ]
    assert lag_argv[8:] == [
        "/opt/kafka/bin/kafka-consumer-groups.sh", "--bootstrap-server", "localhost:9092",
        "--describe", "--group", "shipping-service",
    ]

    tampered = dict(KAFKA_LAG_CONTRACT)
    tampered["consumer_group"] = "order-service"
    rejected = probes.observe(
        registry.bind(
            {"query_id": "kubernetes.kafka_consumer_lag", "parameters": tampered}
        )
    )
    assert rejected["quality"] == "error" and rejected["value"] is None


def test_f12h_fixed_apm_kcm_and_cpu_limit_queries(tmp_path) -> None:
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    registry = ApprovedQueryRegistry.from_path()
    requests = [
        {
            "query_id": "prometheus.apm_service_p95",
            "parameters": {"service_name": "commerce-product"},
        },
        {
            "query_id": "prometheus.apm_service_error_rate",
            "parameters": {"service_name": "commerce-order"},
        },
        {
            "query_id": "prometheus.container_cpu_throttled_time",
            "parameters": F12_PRODUCT_TARGET,
        },
        {
            "query_id": "prometheus.pod_network_error_rate",
            "parameters": {
                "namespace": "rca-testbed-commerce",
                "deployment": "testbed-product",
            },
        },
        {
            "query_id": "kubernetes.deployment_container_cpu_limit",
            "parameters": F12_PRODUCT_TARGET,
        },
    ]

    values = {item["query_id"]: probes.observe(registry.bind(item)) for item in requests}

    assert all(item["quality"] == "good" for item in values.values())
    assert values["kubernetes.deployment_container_cpu_limit"]["value"] == "500m"
    rendered = [
        parse_qs(kwargs["body"].decode())["query"][0]
        for method, url, kwargs in fakes.http_calls
        if method == "POST" and url == PROMETHEUS_URL
    ]
    assert rendered == [
        'max without(grade) (apm.agent.otel.java.percentile95{service_name="commerce-product"})',
        'max without(grade) (apm.agent.otel.java.error_rate{service_name="commerce-order"})',
        'max without(grade) (kcm.pod.cpu_throttled_time{namespace="rca-testbed-commerce",pod=~"testbed-product-.*"})',
        'max without(grade) (kcm.pod.network_rx_error{namespace="rca-testbed-commerce",pod=~"testbed-product-.*"}) + max without(grade) (kcm.pod.network_tx_error{namespace="rca-testbed-commerce",pod=~"testbed-product-.*"})',
    ]

    tampered = dict(F12_PRODUCT_TARGET)
    tampered["deployment"] = "testbed-payment"
    rejected = probes.observe(
        registry.bind(
            {
                "query_id": "kubernetes.deployment_container_cpu_limit",
                "parameters": tampered,
            }
        )
    )
    assert rejected["quality"] == "error" and rejected["value"] is None


def test_gateway_apm_p95_is_an_approved_live_observation(tmp_path) -> None:
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    query = ApprovedQueryRegistry.from_path().bind(
        {
            "query_id": "prometheus.apm_service_p95",
            "parameters": {"service_name": "commerce-gateway"},
        }
    )

    observed = probes.observe(query)

    assert observed["quality"] == "good"
    rendered = parse_qs(fakes.http_calls[-1][2]["body"].decode())["query"][0]
    assert rendered == (
        'max without(grade) '
        '(apm.agent.otel.java.percentile95{service_name="commerce-gateway"})'
    )


def test_restaurant_apm_p95_and_error_rate_are_approved_live_observations(tmp_path) -> None:
    # F02-P (live defect repair) + F24-Q: food-delivery-restaurant was missing
    # from APPROVED_APM_SERVICES even though F02-P's controller already
    # dispatches restaurant_p95 — this closes that gap.
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    registry = ApprovedQueryRegistry.from_path()
    requests = [
        {
            "query_id": "prometheus.apm_service_p95",
            "parameters": {"service_name": "food-delivery-restaurant"},
        },
        {
            "query_id": "prometheus.apm_service_error_rate",
            "parameters": {"service_name": "food-delivery-restaurant"},
        },
    ]

    values = {item["query_id"]: probes.observe(registry.bind(item)) for item in requests}

    assert all(item["quality"] == "good" for item in values.values())


def test_jvm_daemon_thread_count_is_approved_for_f21_targets(tmp_path) -> None:
    # F21-Q/P: no Tomcat-thread-pool metric exists, so the JVM daemon thread sum
    # is the approximation — daemon, because Tomcat's http-nio-*-exec workers are
    # daemon threads. The non-daemon sum this used until 2026-07-28 excluded them
    # and sat flat at 4-5 forever. Allowlisted for order and api only.
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    registry = ApprovedQueryRegistry.from_path()

    order = probes.observe(
        registry.bind(
            {
                "query_id": "prometheus.jvm_daemon_thread_count",
                "parameters": {"service_name": "food-delivery-order"},
            }
        )
    )
    api = probes.observe(
        registry.bind(
            {
                "query_id": "prometheus.jvm_daemon_thread_count",
                "parameters": {"service_name": "core-banking-api"},
            }
        )
    )
    rejected = probes.observe(
        registry.bind(
            {
                "query_id": "prometheus.jvm_daemon_thread_count",
                "parameters": {"service_name": "commerce-order"},
            }
        )
    )

    assert order["quality"] == "good" and api["quality"] == "good"
    assert rejected["quality"] == "error" and rejected["value"] is None
    rendered = parse_qs(fakes.http_calls[-1][2]["body"].decode())["query"][0]
    assert rendered == (
        "sum without(grade,target_id,host_name,process_pid,os_description,os_type,"
        'host_arch,jvm_thread_state) (apm.agent.otel.java.jvm.thread.count'
        '{service_name="core-banking-api",jvm_thread_daemon="true"})'
    )


def test_f06g_pulse_and_payment_duplicate_observations_are_run_scoped(tmp_path) -> None:
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    started = NOW - timedelta(minutes=2)
    _write(
        probes.paths.profile_state / "F06-G-mock-pulse-state.json",
        {
            "scenario_id": "F06-G",
            "started_at": started.isoformat(),
            "last_pulse_at": (NOW - timedelta(seconds=12)).isoformat(),
            "observed_at": NOW.isoformat(),
            "transient_consumed_count": 8,
            "duplicate_expectation_count": 0,
            "expired_unconsumed_count": 0,
            "transient_expectation_absent": False,
            "snapshot_restored": False,
        },
    )
    registry = ApprovedQueryRegistry.from_path()
    query_ids = [
        "mock.transient_consumed_count",
        "mock.pulse_age_seconds",
        "mock.duplicate_expectation_count",
        "mock.expired_unconsumed_count",
        "mock.transient_expectation_absent",
        "mock.snapshot_restored",
        "database.payment_duplicate_order_count_since_t1",
    ]

    values = {
        query_id: probes.observe(
            registry.bind({"query_id": query_id, "parameters": {"scenario_id": "F06-G"}})
        )
        for query_id in query_ids
    }

    assert all(item["quality"] == "good" for item in values.values())
    assert values["mock.transient_consumed_count"]["value"] == 8
    assert values["mock.pulse_age_seconds"]["value"] == 12
    assert values["database.payment_duplicate_order_count_since_t1"]["value"] == 0
    sql, parameters, _ = fakes.database_calls[-1]
    assert sql == PAYMENT_DUPLICATE_SINCE_T1_SQL
    assert parameters == (started.strftime("%Y-%m-%dT%H:%M:%SZ"),)

    rejected = probes.observe(
        registry.bind(
            {
                "query_id": "mock.transient_consumed_count",
                "parameters": {"scenario_id": "F01-H"},
            }
        )
    )
    assert rejected["quality"] == "error" and rejected["value"] is None


def test_f05_payment_cause_and_recovery_observations_are_fixed(tmp_path) -> None:
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    registry = ApprovedQueryRegistry.from_path()
    target = {
        "namespace": "rca-testbed-commerce",
        "deployment": "testbed-payment",
        "container": "payment-service",
    }
    query_ids = [
        "kubernetes.container_restart_count",
        "kubernetes.container_last_termination_reason",
        "kubernetes.container_oom_killed",
        "kubernetes.container_resources_match",
        "kubernetes.container_liveness_probe_match",
        "kubernetes.container_memory_current_bytes",
        "kubernetes.container_memory_limit_bytes",
    ]

    values = {
        query_id: probes.observe(
            registry.bind({"query_id": query_id, "parameters": target})
        )
        for query_id in query_ids
    }

    assert all(item["quality"] == "good" for item in values.values())
    assert values["kubernetes.container_restart_count"]["value"] == 2
    assert values["kubernetes.container_last_termination_reason"]["value"] == "Error"
    assert values["kubernetes.container_oom_killed"]["value"] is False
    assert values["kubernetes.container_resources_match"]["value"] is True
    assert values["kubernetes.container_liveness_probe_match"]["value"] is True
    assert values["kubernetes.container_memory_current_bytes"]["value"] == 574550016
    assert values["kubernetes.container_memory_limit_bytes"]["value"] == 1073741824

    tampered = dict(target)
    tampered["container"] = "order-service"
    rejected = probes.observe(
        registry.bind(
            {"query_id": "kubernetes.container_restart_count", "parameters": tampered}
        )
    )
    assert rejected["quality"] == "error" and rejected["value"] is None


@pytest.mark.asyncio
async def test_failure_and_stale_values_fail_closed_through_observation_adapter(tmp_path) -> None:
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    registry = ApprovedQueryRegistry.from_path()
    query = registry.bind({"query_id": "http.entry_health"})

    healthy = await HttpProbeAdapter(probes.observe, clock=lambda: NOW).observe(query)
    assert healthy.value == 200 and healthy.usable

    document = json.loads(probes.paths.loadgen_summary.read_text())
    del document["entry_status"]
    _write(probes.paths.loadgen_summary, document)
    failed = await HttpProbeAdapter(probes.observe, clock=lambda: NOW).observe(query)
    assert failed.quality == "error" and failed.value is None and not failed.usable

    document["entry_status"] = 200
    document["observed_at"] = (NOW - timedelta(minutes=5)).isoformat()
    _write(probes.paths.loadgen_summary, document)
    stale = await HttpProbeAdapter(probes.observe, clock=lambda: NOW).observe(query)
    assert stale.quality == "error" and stale.value is None and not stale.usable


def test_loadgen_probe_rejects_legacy_summary_shape_without_live_rate(tmp_path) -> None:
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    registry = ApprovedQueryRegistry.from_path()
    document = json.loads(probes.paths.loadgen_summary.read_text())
    document.pop("achieved_rps")
    document["metrics"] = {"iterations": {"rate": 999}}
    _write(probes.paths.loadgen_summary, document)

    observed = probes.observe(registry.bind({"query_id": "loadgen.achieved_rps"}))

    assert observed["quality"] == "error"
    assert observed["value"] is None


def test_new_loadgen_rate_queries_map_to_the_expected_summary_fields(tmp_path) -> None:
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    registry = ApprovedQueryRegistry.from_path()
    document = json.loads(probes.paths.loadgen_summary.read_text())
    document.update({"business_409_rate": 0.4, "business_2xx_rate": 0.6, "read_nonok_rate": 0.9})
    _write(probes.paths.loadgen_summary, document)

    checkout_409 = probes.observe(registry.bind({"query_id": "loadgen.checkout_409_rate"}))
    frozen_completed = probes.observe(registry.bind({"query_id": "loadgen.frozen_bypass_completed_rate"}))
    normal_reject = probes.observe(registry.bind({"query_id": "loadgen.normal_path_reject_rate"}))

    assert checkout_409["quality"] == "good" and checkout_409["value"] == 0.4
    assert frozen_completed["quality"] == "good" and frozen_completed["value"] == 0.6
    assert normal_reject["quality"] == "good" and normal_reject["value"] == 0.9

    with pytest.raises(ObservationContractError):
        registry.bind({"query_id": "loadgen.checkout_409_rate", "parameters": {"step": "checkout"}})


@pytest.mark.parametrize(
    ("resource", "selector"),
    [
        ("testbed-payment", "app=testbed-payment"),
        ("testbed-inventory", "app=testbed-inventory"),
        ("testbed-cart", "app=testbed-cart"),
        # F04-R watches the Kafka StatefulSet pod testbed-kafka-0 via pod_ready.
        ("testbed-kafka-0", "app=testbed-kafka"),
    ],
)
def test_kubernetes_probe_uses_the_allowlisted_scenario_target(
    tmp_path, resource: str, selector: str
) -> None:
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    query = ApprovedQueryRegistry.from_path().bind(
        {
            "query_id": "kubernetes.pod_ready",
            "parameters": {
                "namespace": "rca-testbed-commerce",
                "resource": resource,
            },
        }
    )

    observed = probes.observe(query)

    assert observed["quality"] == "good"
    argv = next(argv for argv, _ in fakes.process_calls if "pods" in argv)
    assert argv[argv.index("--selector") + 1] == selector


@pytest.mark.parametrize(
    ("namespace", "resource", "selector"),
    [
        # F21-P watches the banking api pod (109 kubectl-verified label).
        ("rca-testbed-banking", "testbed-api", "app=testbed-api"),
        # F24-Q (+ F02-P live defect repair) watches the restaurant pod.
        ("rca-testbed-food", "testbed-restaurant", "app=testbed-restaurant"),
    ],
)
def test_kubernetes_probe_uses_the_allowlisted_cross_namespace_target(
    tmp_path, namespace: str, resource: str, selector: str
) -> None:
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    query = ApprovedQueryRegistry.from_path().bind(
        {
            "query_id": "kubernetes.pod_ready",
            "parameters": {"namespace": namespace, "resource": resource},
        }
    )

    observed = probes.observe(query)

    assert observed["quality"] == "good"
    argv = next(argv for argv, _ in fakes.process_calls if "pods" in argv)
    assert argv[argv.index("--selector") + 1] == selector


def test_image_pull_probe_requires_exact_payment_target_and_waiting_reason(tmp_path) -> None:
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    query = ApprovedQueryRegistry.from_path().bind(
        {
            "query_id": "kubernetes.image_pull_failure",
            "parameters": {
                "namespace": "rca-testbed-commerce",
                "resource": "testbed-payment",
                "container": "payment-service",
            },
        }
    )

    observed = probes.observe(query)

    assert observed["quality"] == "good"
    assert observed["value"] is True


def test_image_pull_probe_detects_err_image_never_pull(tmp_path) -> None:
    # ctr-imported local images run with pull policy Never, so a missing tag
    # surfaces as ErrImageNeverPull instead of ImagePullBackOff (F05-G).
    fakes = Fakes()
    inner = fakes.process

    def process(argv, **kwargs):
        result = inner(argv, **kwargs)
        if "pods" in list(argv):
            result = subprocess.CompletedProcess(
                result.args, 0,
                result.stdout.replace("ImagePullBackOff", "ErrImageNeverPull"),
                "",
            )
        return result

    fakes.process = process
    probes = _probes(tmp_path, fakes)
    query = ApprovedQueryRegistry.from_path().bind(
        {
            "query_id": "kubernetes.image_pull_failure",
            "parameters": {
                "namespace": "rca-testbed-commerce",
                "resource": "testbed-payment",
                "container": "payment-service",
            },
        }
    )

    observed = probes.observe(query)

    assert observed["quality"] == "good"
    assert observed["value"] is True


def test_readable_no_effect_attempt_does_not_poison_clean_window(tmp_path) -> None:
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    run_dir = probes.paths.runs / "blocked-before-apply"
    _write(run_dir / "timeline.json", {"schema_version": 1, "level_changes": []})
    _write(run_dir / "state.json", {"status": "blocked", "level_changes": []})

    evidence = probes.inspect(_eligibility(checks=["clean-window"]))

    assert evidence.check_results["clean-window"] is True
    assert evidence.overlapping_run_ids == []


def test_live_loadgen_fallback_reads_only_tagged_remote_artifact(tmp_path) -> None:
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    probes.paths.loadgen_summary.unlink()
    registry = ApprovedQueryRegistry.from_path()

    status = probes.observe(registry.bind({"query_id": "http.entry_health"}))
    business = probes.observe(
        registry.bind(
            {"query_id": "business.checkout_invariant", "parameters": {"business_key": "checkout"}}
        )
    )
    achieved = probes.observe(registry.bind({"query_id": "loadgen.achieved_rps"}))

    assert status["value"] == 429 and business["value"] is False
    assert achieved["value"] == 71.0
    ssh_calls = [argv for argv, _ in fakes.process_calls if argv[0] == "ssh"]
    assert all(argv[-3:] == ["cat", "--", "/tmp/rca-scenario-F07-H-live.json"] for argv in ssh_calls)


def test_snapshot_producer_atomically_refreshes_legacy_artifacts(tmp_path) -> None:
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    evidence_path = tmp_path / "out" / "baseline-evidence.json"
    observations_path = tmp_path / "out" / "observations.json"
    producer = SnapshotProducer(
        probes,
        registry=ApprovedQueryRegistry.from_path(),
        evidence_path=evidence_path,
        observation_path=observations_path,
    )
    producer.refresh(
        _eligibility(checks=["baseline-traffic", "target-health"]),
        [{"query_id": "http.entry_health"}],
    )

    assert json.loads(evidence_path.read_text())["source"] == "live-probes:v1"
    snapshot = json.loads(observations_path.read_text())
    assert snapshot["queries"]["http.entry_health"]["quality"] == "good"
    assert not list((tmp_path / "out").glob(".*"))


def test_f15r_flap_and_f15t1_food_payment_observations(tmp_path) -> None:
    fakes = Fakes()
    probes = _probes(tmp_path, fakes)
    started = NOW - timedelta(minutes=3)
    _write(
        probes.paths.profile_state / "F15-R-mock-flap-state.json",
        {
            "scenario_id": "F15-R",
            "started_at": started.isoformat(),
            "observed_at": NOW.isoformat(),
            "episode": 1,
            "fault_active": True,
            "worker_active": True,
        },
    )
    registry = ApprovedQueryRegistry.from_path()

    flap = {
        query_id: probes.observe(
            registry.bind({"query_id": query_id, "parameters": {"scenario_id": "F15-R"}})
        )
        for query_id in (
            "scenario.mock_flap_episode",
            "scenario.mock_flap_fault_active",
            "business.order_duplicate_count_since_t1",
        )
    }
    assert all(item["quality"] == "good" for item in flap.values())
    assert flap["scenario.mock_flap_episode"]["value"] == 1
    assert flap["scenario.mock_flap_fault_active"]["value"] is True
    assert flap["business.order_duplicate_count_since_t1"]["value"] == 0
    sql, parameters, _ = fakes.database_calls[-1]
    assert sql == PAYMENT_DUPLICATE_SINCE_T1_SQL
    assert parameters == (started.strftime("%Y-%m-%dT%H:%M:%SZ"),)

    rejected = probes.observe(
        registry.bind(
            {"query_id": "scenario.mock_flap_episode", "parameters": {"scenario_id": "F06-G"}}
        )
    )
    assert rejected["quality"] == "error" and rejected["value"] is None

    food_target = {
        "namespace": "rca-testbed-food",
        "deployment": "testbed-payment",
        "container": "payment-service",
    }
    food = {
        query_id: probes.observe(
            registry.bind({"query_id": query_id, "parameters": food_target})
        )
        for query_id in (
            "kubernetes.container_restart_count",
            "kubernetes.container_last_termination_reason",
            "kubernetes.deployment_container_memory_limit",
        )
    }
    assert all(item["quality"] == "good" for item in food.values())
    assert food["kubernetes.container_restart_count"]["value"] == 2
    assert food["kubernetes.container_last_termination_reason"]["value"] == "Error"
    assert food["kubernetes.deployment_container_memory_limit"]["value"] == "1Gi"

    ready = probes.observe(
        registry.bind(
            {
                "query_id": "kubernetes.pod_ready",
                "parameters": {"namespace": "rca-testbed-food", "resource": "testbed-payment"},
            }
        )
    )
    assert ready["quality"] == "good" and ready["value"] is True

    denied = probes.observe(
        registry.bind(
            {"query_id": "kubernetes.container_resources_match", "parameters": food_target}
        )
    )
    assert denied["quality"] == "error" and denied["value"] is None

    tagged = probes.observe(
        registry.bind(
            {
                "query_id": "database.tagged_session_count",
                "parameters": {"scenario_tag": "rca-F15-T1-inventory-lock"},
            }
        )
    )
    assert tagged["quality"] == "good" and tagged["value"] == 3
