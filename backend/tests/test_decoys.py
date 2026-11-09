"""Decoy and routine deployments inside the recording cycle (app.decoys, U7)."""
from __future__ import annotations

import json
import subprocess
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.decoys import (
    DecoyPolicy,
    DecoyRecord,
    KubectlDecoyExecutor,
    build_schedule,
    classify_targets,
    cycle_seed,
    load_decoy_policy,
    partition_decoy_alarms,
)

CYCLE_START = datetime(2026, 11, 10, 1, 0, tzinfo=timezone.utc)
BUFFER_START = CYCLE_START + timedelta(hours=2)


def _target(namespace, deployment, service, target_id, *, rollout_ok=True, aliases=()):
    return {
        "namespace": namespace,
        "deployment": deployment,
        "container": deployment.removeprefix("testbed-") + "-service",
        "service_name": service,
        "target_id": target_id,
        "rollout_ok": rollout_ok,
        "aliases": list(aliases),
    }


def policy_document(**overrides) -> dict:
    document = {
        "schema_version": 1,
        "_note": "test",
        "windows": {
            "normal": {
                "start_after_cycle_start_sec": 600,
                "end_before_buffer_sec": 2700,
                "count_weights": {"0": 25, "1": 45, "2": 25, "3": 5},
            },
            "near_fault": {
                "start_before_buffer_sec": 2700,
                "end_before_buffer_sec": 720,
                "count_weights": {"0": 50, "1": 40, "2": 10},
            },
        },
        "min_normal_sec": 3600,
        "min_gap_sec": 720,
        "late_limit_sec": 600,
        "rollout_timeout_sec": 420,
        "alarm_excuse_grace_sec": 900,
        "kind_weights": {"restart": 45, "config_rollout": 15, "configmap": 25, "annotation": 15},
        "relation_weights": {"on_path": 35, "same_domain": 30, "off_domain": 35},
        "configmap": {"name": "service-config", "key": "CONFIG_REVISION"},
        "annotation_key": "kubernetes.io/change-cause",
        "scenario_exclusions": {},
        "domain_namespaces": {
            "commerce": "rca-testbed-commerce",
            "core-banking": "rca-testbed-banking",
        },
        "targets": [
            _target("rca-testbed-commerce", "testbed-inventory", "commerce-inventory", "uuid-inv"),
            _target("rca-testbed-commerce", "testbed-order", "commerce-order", "uuid-order"),
            _target("rca-testbed-commerce", "testbed-user", "commerce-user", "uuid-user", rollout_ok=False),
            _target("rca-testbed-banking", "testbed-transfer", "core-banking-transfer", "uuid-tr",
                    rollout_ok=False, aliases=["banking-transfer"]),
            _target("rca-testbed-banking", "testbed-ledger", "core-banking-ledger", "uuid-led",
                    aliases=["banking-ledger"]),
        ],
    }
    document.update(overrides)
    return document


def policy(**overrides) -> DecoyPolicy:
    return DecoyPolicy.model_validate(policy_document(**overrides))


SCENARIO = {
    "domain": "commerce",
    "root_cause": {
        "target_id": "commerce-postgres:inventory_schema.inventory",
        "trigger_target_id": "commerce-inventory",
    },
    "scoring": {"accept": ["commerce-postgres:inventory_schema.inventory"], "partial": ["commerce-inventory"]},
}


def schedule(seed: int, *, scenario=SCENARIO, active=None, **kwargs) -> list[DecoyRecord]:
    return build_schedule(
        active or policy(),
        seed=seed,
        scenario_id="F01-R",
        scenario=scenario,
        cycle_start=kwargs.get("cycle_start", CYCLE_START),
        buffer_start=kwargs.get("buffer_start", BUFFER_START),
    )


def _at(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# --- schedule -------------------------------------------------------------
def test_same_seed_gives_the_same_schedule_and_another_seed_differs() -> None:
    first = [record.model_dump() for record in schedule(1234)]
    again = [record.model_dump() for record in schedule(1234)]
    others = {json.dumps([r.model_dump() for r in schedule(seed)]) for seed in range(20)}

    assert first == again
    assert len(others) > 10


def test_cycle_seed_is_reproducible_from_recorded_values() -> None:
    seed = cycle_seed("base", "cycle-1-abc", "F01-R", "2026-11-10T01:00:00Z")

    assert seed == cycle_seed("base", "cycle-1-abc", "F01-R", "2026-11-10T01:00:00Z")
    assert seed != cycle_seed("base", "cycle-1-abc", "F01-R", "2026-11-10T04:00:00Z")
    assert 0 <= seed < 2**63


def test_schedule_draws_vary_count_time_target_kind_and_relation() -> None:
    """The shortcut is learned from anything fixed, so every axis must vary
    across cycles: count (zero included), time, target, kind, on/off path."""
    counts, kinds, relations, windows, targets = Counter(), Counter(), Counter(), Counter(), Counter()
    for seed in range(400):
        records = schedule(seed)
        counts[len(records)] += 1
        for record in records:
            kinds[record.kind] += 1
            relations[record.relation] += 1
            windows[record.window] += 1
            targets[record.deployment or record.configmap] += 1

    assert counts[0] > 0 and max(counts) >= 3
    assert set(kinds) == {"restart", "config_rollout", "configmap", "annotation"}
    assert set(relations) == {"on_path", "same_domain", "off_domain"}
    assert set(windows) == {"normal", "near_fault"}
    assert len(targets) >= 4


def test_schedule_respects_windows_gap_and_never_reaches_the_buffer() -> None:
    active = policy()
    near = active.windows["near_fault"]
    for seed in range(300):
        records = schedule(seed)
        times = [_at(record.planned_at) for record in records]
        assert times == sorted(times)
        assert all(b - a >= timedelta(seconds=active.min_gap_sec) for a, b in zip(times, times[1:]))
        for record, at in zip(records, times):
            assert CYCLE_START + timedelta(seconds=600) <= at
            # A rollout started at the latest moment still ends before the buffer.
            assert at + timedelta(seconds=active.rollout_timeout_sec) < BUFFER_START
            if record.window == "near_fault":
                assert BUFFER_START - timedelta(seconds=near.start_before_buffer_sec) <= at
                assert at <= BUFFER_START - timedelta(seconds=near.end_before_buffer_sec)


def test_schedule_only_uses_rollout_free_kinds_on_maxsurge_zero_targets() -> None:
    for seed in range(400):
        for record in schedule(seed):
            if record.deployment in {"testbed-user", "testbed-transfer"}:
                assert record.kind in {"configmap", "annotation"}


def test_on_path_comes_from_the_answer_key_names_and_aliases() -> None:
    groups, namespaces = classify_targets(policy(), SCENARIO)
    assert [t.deployment for t in groups["on_path"]] == ["testbed-inventory"]
    assert {t.deployment for t in groups["same_domain"]} == {"testbed-order", "testbed-user"}
    assert namespaces == {"rca-testbed-commerce"}

    banking = {"domain": "core-banking", "scoring": {"accept": ["banking-transfer"]}}
    groups, _ = classify_targets(policy(), banking)
    assert [t.deployment for t in groups["on_path"]] == ["testbed-transfer"]


def test_schedule_is_empty_for_a_shortened_smoke_cycle_and_honours_exclusions() -> None:
    assert schedule(7, buffer_start=CYCLE_START + timedelta(minutes=20)) == []

    excluded = policy(scenario_exclusions={"F01-R": ["rca-testbed-commerce/testbed-inventory"]})
    for seed in range(200):
        assert all(r.deployment != "testbed-inventory" for r in schedule(seed, active=excluded))


def test_policy_refuses_a_rollout_timeout_that_reaches_the_buffer() -> None:
    with pytest.raises(ValueError, match="before the buffer"):
        policy(rollout_timeout_sec=900)


# --- executor -------------------------------------------------------------
class Recorder:
    def __init__(self, stdout="", returncode=0):
        self.calls: list[list[str]] = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "boom")


def _record(**overrides) -> DecoyRecord:
    base = dict(
        id="d1", window="normal", planned_at="2026-11-10T01:20:00Z", kind="restart",
        namespace="rca-testbed-commerce", deployment="testbed-order",
        service_name="commerce-order", target_id="uuid-order", relation="same_domain",
        value="202611100120-abcdef",
    )
    base.update(overrides)
    return DecoyRecord(**base)


def test_executor_commands_never_touch_an_image() -> None:
    runner = Recorder()
    executor = KubectlDecoyExecutor(policy(), process_runner=runner)

    executor.apply(_record(kind="config_rollout", configmap="service-config"))
    executor.apply(_record(kind="annotation", value="revision abcdef"))
    executor.apply(_record(kind="configmap", deployment=None, configmap="service-config"))

    flat = [" ".join(call) for call in runner.calls]
    assert flat[0].endswith(
        'patch configmap service-config --type merge -p {"data": {"CONFIG_REVISION": "202611100120-abcdef"}}'
    )
    assert flat[1].endswith("rollout restart deployment/testbed-order")
    assert flat[2].endswith("annotate deployment/testbed-order kubernetes.io/change-cause=revision abcdef --overwrite")
    assert flat[3].startswith("kubectl --kubeconfig /root/tb-kubeconfig -n rca-testbed-commerce patch configmap")
    assert not any("set image" in line or "image" in call for line, call in zip(flat, runner.calls))


def test_executor_raises_on_kubectl_failure_and_reads_rollout_from_status() -> None:
    with pytest.raises(RuntimeError, match="rollout failed"):
        KubectlDecoyExecutor(policy(), process_runner=Recorder(returncode=1)).apply(_record())

    done = {"metadata": {"generation": 3}, "spec": {"replicas": 1},
            "status": {"observedGeneration": 3, "updatedReplicas": 1, "availableReplicas": 1, "replicas": 1}}
    surging = {**done, "status": {**done["status"], "replicas": 2}}
    assert KubectlDecoyExecutor(policy(), process_runner=Recorder(json.dumps(done))).rollout_done(_record())
    assert not KubectlDecoyExecutor(policy(), process_runner=Recorder(json.dumps(surging))).rollout_done(_record())


# --- preflight alarm accounting --------------------------------------------
def _alert(labels, active_at="2026-11-10T02:30:30Z"):
    return {"state": "firing", "labels": labels, "activeAt": active_at}


APPLIED = _record(
    planned_at="2026-11-10T02:30:00Z", status="applied",
    started_at="2026-11-10T02:30:00Z", finished_at="2026-11-10T02:32:00Z",
)


def test_alarm_from_a_finished_decoy_is_excused() -> None:
    alerts = [
        _alert({"alertname": "latency", "service_name": "commerce-order"}),
        _alert({"alertname": "pod", "namespace": "rca-testbed-commerce", "pod": "testbed-order-5d9-xk2"},
               active_at="2026-11-10T02:31:10.123456789Z"),
        _alert({"alertname": "x", "target_id": "uuid-order"}),
    ]

    remaining, excused = partition_decoy_alarms(alerts, [APPLIED], grace_sec=900)

    assert remaining == []
    assert [item["decoy_id"] for item in excused] == ["d1", "d1", "d1"]


def test_real_residue_still_blocks() -> None:
    """Pair of the test above: what the decoy does not explain keeps blocking."""
    other_service = _alert({"service_name": "commerce-payment"})
    before_decoy = _alert({"service_name": "commerce-order"}, active_at="2026-11-10T02:20:00Z")
    long_after = _alert({"service_name": "commerce-order"}, active_at="2026-11-10T03:00:00Z")
    same_name_other_ns = _alert({"namespace": "rca-testbed-food", "pod": "testbed-order-1"})
    no_time = {"state": "firing", "labels": {"service_name": "commerce-order"}}
    alerts = [other_service, before_decoy, long_after, same_name_other_ns, no_time]

    remaining, excused = partition_decoy_alarms(alerts, [APPLIED], grace_sec=900)
    assert remaining == alerts and excused == []

    for status in ("rolled_back", "failed", "rolling", "planned", "skipped"):
        unfinished = APPLIED.model_copy(update={"status": status})
        remaining, excused = partition_decoy_alarms(
            [_alert({"service_name": "commerce-order"})], [unfinished], grace_sec=900
        )
        assert len(remaining) == 1 and excused == [], status


# --- the real registry (cross-repo contract) -------------------------------
def test_the_testbed_registry_policy_plans_for_every_scenario() -> None:
    root = Path(__file__).resolve().parents[2].parent / "testbed-services/scripts/scenarios/registry"
    if not (root / "decoy-deployments.json").is_file():
        pytest.skip("testbed-services registry is not checked out next to the runner")
    active, digest = load_decoy_policy(root / "decoy-deployments.json")
    scenarios = json.loads((root / "scenario-metadata.json").read_text(encoding="utf-8"))["scenarios"]

    assert len(digest) == 64
    for scenario_id, scenario in scenarios.items():
        for seed in range(3):
            build_schedule(
                active, seed=seed, scenario_id=scenario_id, scenario=scenario,
                cycle_start=CYCLE_START, buffer_start=BUFFER_START,
            )
