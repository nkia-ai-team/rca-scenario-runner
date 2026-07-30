from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.adaptive import ObservedValue
from app.observations import (
    APPROVED_ADAPTERS,
    ApprovedQueryRegistry,
    BusinessProbeAdapter,
    CaptureStatusAdapter,
    ClickHouseAdapter,
    DatabaseAdapter,
    HttpProbeAdapter,
    HostProbeAdapter,
    KubernetesAdapter,
    LoadgenSummaryAdapter,
    ObservationBlocked,
    ObservationContractError,
    ObservationPoller,
    PrometheusAdapter,
)


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
REGISTRY_PATH = Path(__file__).parents[1] / "app" / "observation_queries.json"
ADAPTER_CLASSES = {
    "loadgen_summary": LoadgenSummaryAdapter,
    "http_probe": HttpProbeAdapter,
    "prometheus": PrometheusAdapter,
    "kubernetes": KubernetesAdapter,
    "database": DatabaseAdapter,
    "host_probe": HostProbeAdapter,
    "business_probe": BusinessProbeAdapter,
    "capture_status": CaptureStatusAdapter,
    "clickhouse": ClickHouseAdapter,
}
QUERY_BY_ADAPTER = {
    "loadgen_summary": "loadgen.achieved_rps",
    "http_probe": "http.entry_health",
    "prometheus": "prometheus.user_p95",
    "kubernetes": "kubernetes.pod_ready",
    "database": "database.tagged_session_count",
    "host_probe": "host.scenario_clean",
    "business_probe": "business.checkout_invariant",
    "capture_status": "capture.export_complete",
    "clickhouse": "clickhouse.service_error_rate",
}


def _parameters(adapter_id: str) -> dict[str, str]:
    return {
        "prometheus": {"service_name": "checkout"},
        "kubernetes": {"namespace": "commerce", "resource": "pod/x"},
        "database": {"scenario_tag": "F01-R"},
        "host_probe": {"scenario_id": "F02-H"},
        "business_probe": {"business_key": "order-1"},
        "capture_status": {"run_id": "run-1"},
        "clickhouse": {"service_name": "commerce-payment"},
    }.get(adapter_id, {})


def _adapters(*, observed_at: datetime = NOW, quality: str = "good"):
    result = {}
    for adapter_id, adapter_class in ADAPTER_CLASSES.items():
        async def reader(query, *, source=adapter_id):
            return {
                "value": True,
                "observed_at": observed_at,
                "source": f"fake:{source}",
                "quality": quality,
            }

        result[adapter_id] = adapter_class(reader, clock=lambda: NOW)
    return result


@pytest.mark.parametrize("adapter_id", sorted(APPROVED_ADAPTERS))
async def test_all_approved_adapters_produce_adaptive_observed_values(adapter_id: str) -> None:
    registry = ApprovedQueryRegistry.from_path()
    query = registry.bind({
        "query_id": QUERY_BY_ADAPTER[adapter_id],
        "parameters": _parameters(adapter_id),
    })
    observed = await _adapters()[adapter_id].observe(query)

    assert isinstance(observed, ObservedValue)
    assert observed.observed_at == NOW
    assert observed.source == f"fake:{adapter_id}"
    assert observed.freshness == "fresh"
    assert observed.quality == "good"
    assert observed.usable


def test_registry_rejects_raw_scenario_query_text_and_unapproved_parameters() -> None:
    registry = ApprovedQueryRegistry.from_path()
    for forbidden in ("promql", "query", "query_text", "promql_template"):
        with pytest.raises(ObservationContractError, match="raw scenario query text"):
            registry.bind({"query_id": "prometheus.user_p95", forbidden: "up"})
    with pytest.raises(ObservationContractError, match="not allowlisted"):
        registry.bind({
            "query_id": "prometheus.user_p95",
            "parameters": {"service_name": "checkout", "cluster": "other"},
        })


def test_registry_rejects_raw_query_definitions_and_requires_prometheus_template() -> None:
    document = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    bad = copy.deepcopy(document)
    bad["queries"]["prometheus.user_p95"]["promql"] = "up"
    with pytest.raises(ObservationContractError, match="forbidden raw query fields"):
        ApprovedQueryRegistry(bad)

    bad = copy.deepcopy(document)
    del bad["queries"]["prometheus.user_p95"]["template_id"]
    with pytest.raises(ObservationContractError, match="approved template_id"):
        ApprovedQueryRegistry(bad)

    bad = copy.deepcopy(document)
    del bad["queries"]["capture.export_complete"]
    with pytest.raises(ObservationContractError, match="cover exactly"):
        ApprovedQueryRegistry(bad)


async def test_poller_normalizes_all_adapters_without_external_access() -> None:
    poller = ObservationPoller(ApprovedQueryRegistry.from_path(), _adapters())
    requests = [
        {"query_id": QUERY_BY_ADAPTER[adapter], "parameters": _parameters(adapter)}
        for adapter in sorted(APPROVED_ADAPTERS)
    ]
    values = await poller.poll(requests)
    assert set(values) == set(QUERY_BY_ADAPTER.values())
    assert all(value.usable and value.observed_at == NOW for value in values.values())


@pytest.mark.parametrize(
    ("adapters", "reason"),
    [
        (_adapters(observed_at=NOW - timedelta(minutes=10)), "stale/good"),
        (_adapters(quality="error"), "fresh/error"),
    ],
)
async def test_poller_blocks_stale_and_error_observations(adapters, reason: str) -> None:
    poller = ObservationPoller(ApprovedQueryRegistry.from_path(), adapters)
    with pytest.raises(ObservationBlocked, match=reason):
        await poller.poll([{"query_id": "http.entry_health"}])


async def test_reader_failure_becomes_blocking_error_not_an_external_retry() -> None:
    async def failing_reader(query):
        raise RuntimeError("fake dependency failed")

    adapters = _adapters()
    adapters["http_probe"] = HttpProbeAdapter(failing_reader, clock=lambda: NOW)
    poller = ObservationPoller(ApprovedQueryRegistry.from_path(), adapters)
    with pytest.raises(ObservationBlocked) as exc_info:
        await poller.poll([{"query_id": "http.entry_health"}])
    blocked = exc_info.value.blocked["http.entry_health"]
    assert blocked.quality == "error"
    assert blocked.value is None
