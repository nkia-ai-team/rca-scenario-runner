"""Decoy and routine deployments inside the recording cycle (app.decoys, U7)."""
from __future__ import annotations

import json
import re
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
    cycle_seed,
    decoy_candidates,
    excluded_targets,
    load_decoy_policy,
    mentioned_targets,
    partition_decoy_alarms,
    stated_unchanged,
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
        # All kinds on here so every kind's path is exercised; the shipped
        # policy enables only restart (test against the real registry below).
        "relation_weights": {"on_path": 0, "same_domain": 50, "off_domain": 50},
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


def test_schedule_draws_vary_count_time_target_and_kind() -> None:
    """The shortcut is learned from anything fixed, so every axis must vary
    across cycles: count (zero included), time, target, kind, and both
    off-path relations. On-path never appears (user decision 2026-11-09)."""
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
    assert set(relations) == {"same_domain", "off_domain"}
    assert set(windows) == {"normal", "near_fault"}
    assert len(targets) >= 3


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


def test_fault_path_and_unchanged_targets_never_carry_a_decoy() -> None:
    """Scored names and any target the answer key mentions are the fault path;
    a target the answer key says did not change is out too."""
    scenario = {
        **SCENARIO,
        "must_rule_out": ["order 쪽 변경이 아님: order 에는 롤아웃이 없다"],
        "related_scenarios": {"contrast_with": ["F99 (ledger 롤아웃 없음)"]},
    }
    reasons = excluded_targets(policy(), "F01-R", scenario)
    assert reasons == {
        "rca-testbed-commerce/testbed-inventory": "scored",
        "rca-testbed-commerce/testbed-order": "stated_unchanged",
    }
    victim = {**SCENARIO, "user_impact": "checkouts fail; commerce-user logins slow down"}
    assert excluded_targets(policy(), "F01-R", victim)["rca-testbed-commerce/testbed-user"] == "mentioned"
    assert "rca-testbed-commerce/testbed-user" not in excluded_targets(
        policy(path_scope="scored"), "F01-R", victim
    )
    groups = decoy_candidates(policy(), "F01-R", scenario)
    assert groups["on_path"] == []
    assert {t.deployment for t in groups["same_domain"]} == {"testbed-user"}
    assert {t.deployment for t in groups["off_domain"]} == {"testbed-transfer", "testbed-ledger"}
    for seed in range(300):
        for record in schedule(seed, scenario=scenario):
            assert record.deployment not in {"testbed-inventory", "testbed-order"}
            assert record.relation != "on_path"


def test_no_change_sentences_are_read_mechanically() -> None:
    active = policy()
    named = {"domain": "core-banking", "must_support": ["transfer, ledger 에 롤아웃이 없다."]}
    assert set(stated_unchanged(active, named)) == {
        "rca-testbed-banking/testbed-transfer", "rca-testbed-banking/testbed-ledger",
    }
    whole_namespace = {"domain": "commerce", "description": "no rollout or restart happens in rca-testbed-banking."}
    assert set(stated_unchanged(active, whole_namespace)) == {
        "rca-testbed-banking/testbed-transfer", "rca-testbed-banking/testbed-ledger",
    }
    generic = {"domain": "commerce", "description": "Nothing else changes: no rollout, no restart."}
    assert set(stated_unchanged(active, generic)) == {
        "rca-testbed-commerce/testbed-inventory", "rca-testbed-commerce/testbed-order",
        "rca-testbed-commerce/testbed-user",
    }
    store_only = {"domain": "commerce", "must_rule_out": ["PostgreSQL 은 스키마 변경이 없다"]}
    assert stated_unchanged(active, store_only) == {}
    other_scenario = {"domain": "commerce", "related_scenarios": {"contrast_with": ["order 롤아웃 없음"]}}
    assert stated_unchanged(active, other_scenario) == {}


def test_policy_refuses_on_path_decoys() -> None:
    with pytest.raises(ValueError, match="fault path"):
        policy(relation_weights={"on_path": 10, "same_domain": 50, "off_domain": 50})


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
PREFLIGHT = datetime(2026, 11, 10, 2, 40, tzinfo=timezone.utc)
BUFFER = datetime(2026, 11, 10, 2, 38, tzinfo=timezone.utc)


def _partition(alerts, decoys=(APPLIED,), *, now=PREFLIGHT, buffer_start=BUFFER):
    return partition_decoy_alarms(alerts, list(decoys), grace_sec=900, now=now, buffer_start=buffer_start)


def test_alarm_from_a_finished_decoy_rollout_is_excused() -> None:
    alerts = [
        _alert({"alertname": "latency", "service_name": "commerce-order"}),
        _alert({"alertname": "pod", "namespace": "rca-testbed-commerce", "pod": "testbed-order-5d9-xk2"},
               active_at="2026-11-10T02:31:10.123456789Z"),
        _alert({"alertname": "x", "target_id": "uuid-order"}),
        _alert({"alertname": "w", "namespace": "rca-testbed-commerce", "deployment": "testbed-order"}),
    ]

    remaining, excused = _partition(alerts)

    assert remaining == []
    assert [item["decoy_id"] for item in excused] == ["d1", "d1", "d1", "d1"]


def test_real_residue_still_blocks() -> None:
    """Pair of the test above, including the review's cases (U7-review B1, B2, M1, M2)."""
    cases = {
        "other service": _alert({"service_name": "commerce-payment"}),
        "began before the decoy, even by a second": _alert(
            {"service_name": "commerce-order"}, active_at="2026-11-10T02:29:59Z"),
        "began long after the decoy": _alert(
            {"service_name": "commerce-order"}, active_at="2026-11-10T02:50:00Z"),
        "began inside the buffer": _alert(
            {"service_name": "commerce-order"}, active_at="2026-11-10T02:38:30Z"),
        "names the target only in a peer label": _alert(
            {"service_name": "commerce-gateway", "peer_service": "commerce-order"}),
        "same Deployment name in another namespace": _alert(
            {"namespace": "rca-testbed-food", "pod": "testbed-order-1"}),
        "no start time": {"state": "firing", "labels": {"service_name": "commerce-order"}},
    }
    for name, alert in cases.items():
        remaining, excused = _partition([alert])
        assert remaining == [alert] and excused == [], name

    # Still firing long after a harmless restart should have settled (B2).
    late = datetime(2026, 11, 10, 2, 48, tzinfo=timezone.utc)
    assert _partition([_alert({"service_name": "commerce-order"})], now=late)[1] == []

    # A metadata-only decoy cannot cause an alarm (B1).
    for kind in ("annotation", "configmap"):
        quiet = APPLIED.model_copy(update={"kind": kind})
        assert _partition([_alert({"service_name": "commerce-order"})], [quiet])[1] == [], kind

    for status in ("rolled_back", "failed", "rolling", "planned", "skipped"):
        unfinished = APPLIED.model_copy(update={"status": status})
        remaining, excused = _partition([_alert({"service_name": "commerce-order"})], [unfinished])
        assert len(remaining) == 1 and excused == [], status


# --- the real registry (cross-repo contract) -------------------------------
def test_the_testbed_registry_policy_plans_for_every_scenario() -> None:
    root = Path(__file__).resolve().parents[2].parent / "testbed-services/scripts/scenarios/registry"
    if not (root / "decoy-deployments.json").is_file():
        pytest.skip("testbed-services registry is not checked out next to the runner")
    active, digest = load_decoy_policy(root / "decoy-deployments.json")
    scenarios = json.loads((root / "scenario-metadata.json").read_text(encoding="utf-8"))["scenarios"]

    assert len(digest) == 64
    # P4 not confirmed yet: the shipped policy rolls pods only by restart. The VM testbed kinds (review rv-t4o M3,
    # T4U) are the only others, and only VM targets take them.
    from app.decoys import VM_KINDS

    enabled = {kind for kind, weight in active.kind_weights.items() if weight > 0}
    assert "restart" in enabled and enabled - {"restart"} <= VM_KINDS
    vm_kinds = {target.key: set(target.kinds) for target in active.targets if target.transport == "vm"}
    for scenario_id, scenario in scenarios.items():
        excluded = excluded_targets(active, scenario_id, scenario)
        for seed in range(3):
            for record in build_schedule(
                active, seed=seed, scenario_id=scenario_id, scenario=scenario,
                cycle_start=CYCLE_START, buffer_start=BUFFER_START,
            ):
                key = f"{record.namespace}/{record.deployment}"
                if record.node is None:
                    assert record.kind == "restart"
                else:
                    assert record.kind in vm_kinds[key], (key, record.kind)
                assert key not in excluded, scenario_id


# --- U7 수정 2회차: review2 B3, B4, M3 -------------------------------------
REGISTRY = Path(__file__).resolve().parents[2].parent / "testbed-services/scripts/scenarios/registry"


def _real():
    if not (REGISTRY / "decoy-deployments.json").is_file():
        pytest.skip("testbed-services registry is not checked out next to the runner")
    active, _ = load_decoy_policy(REGISTRY / "decoy-deployments.json")
    scenarios = json.loads((REGISTRY / "scenario-metadata.json").read_text(encoding="utf-8"))["scenarios"]
    return active, scenarios


def _candidate_keys(active, scenario_id, scenario, scope):
    scoped = active.model_copy(update={"path_scope": scope})
    return {t.key for group in decoy_candidates(scoped, scenario_id, scenario).values() for t in group}


def test_a_victim_in_another_namespace_named_by_a_bare_word_is_on_the_path() -> None:
    """review2 B3: a bank-caused outage whose answer key names commerce victims
    only by bare words (order-service, gateway, a path listing) must not get a
    commerce decoy."""
    scenario = {
        "domain": "core-banking",
        "scoring": {"accept": ["core-banking-ledger"]},
        "description": "order-service answers checkout with 502 through the gateway.",
        "story_ko": {"design_notes": ["경로: 체크아웃 gateway → order → inventory → payment"]},
    }
    for scope in ("mentioned", "scored"):
        keys = _candidate_keys(policy(), "X", scenario, scope)
        if scope == "mentioned":
            assert "rca-testbed-commerce/testbed-order" not in keys
            assert "rca-testbed-commerce/testbed-inventory" not in keys
        assert "rca-testbed-banking/testbed-ledger" not in keys


def test_scored_items_exclude_every_target_they_name_and_no_change_lists_are_read() -> None:
    """review2 B4: Korean restart words, lists and particles. And under the
    scored scope a target named in must_support or must_rule_out is out
    whatever the sentence says (excluding too much is the safe side)."""
    sentences = [
        "rca-testbed-commerce 에 Deployment 롤아웃, 새 ReplicaSet, 파드 재시작이 없다(KCM).",
        "order, inventory 는 롤아웃과 오류가 없고 Ready 다.",
        "order 배포나 재시작이 아님",
        "order 는 롤아웃이나 재시작이 없고 Ready 다",
        "같은 네임스페이스의 order, inventory 는 재시작 없이 Ready 다",
        "order 재기동 없음",
    ]
    for sentence in sentences:
        assert "rca-testbed-commerce/testbed-order" in stated_unchanged(
            policy(), {"domain": "commerce", "description": sentence}
        ), sentence
    scenario = {
        "domain": "commerce",
        "scoring": {"accept": ["commerce-user"]},
        "must_rule_out": ["게이트웨이 설정 오배포가 아님: 게이트웨이 롤아웃이 없고 user 가 Ready"],
        "must_support": ["order 지연이 먼저 보인다"],
    }
    keys = _candidate_keys(policy(), "X", scenario, "scored")
    assert "rca-testbed-commerce/testbed-order" not in keys


# --- U7b: short words held to the domain and the app-name boundary ---------
def _three_domains() -> DecoyPolicy:
    return policy(
        domain_namespaces={
            "commerce": "rca-testbed-commerce",
            "core-banking": "rca-testbed-banking",
            "food-delivery": "rca-testbed-food",
        },
        targets=[
            _target("rca-testbed-commerce", "testbed-order", "commerce-order", "uuid-c-order"),
            _target("rca-testbed-commerce", "testbed-payment", "commerce-payment", "uuid-c-pay"),
            {**_target("rca-testbed-commerce", "testbed-gateway", "commerce-gateway", "uuid-gw"),
             "container": "api-gateway"},
            _target("rca-testbed-banking", "testbed-api", "core-banking-api", "uuid-api", aliases=["banking-api"]),
            _target("rca-testbed-banking", "testbed-ledger", "core-banking-ledger", "uuid-led",
                    aliases=["banking-ledger"]),
            _target("rca-testbed-food", "testbed-order", "food-delivery-order", "uuid-f-order",
                    aliases=["food-order"]),
            _target("rca-testbed-food", "testbed-payment", "food-delivery-payment", "uuid-f-pay",
                    aliases=["food-payment"]),
        ],
    )


def test_a_url_prefix_does_not_name_another_domains_api() -> None:
    """D1 review N2: a commerce answer key citing ``/api/orders`` excluded
    banking ``testbed-api``. The first segment of a route is the URL prefix;
    later segments and the app's own domain still count."""
    active = _three_domains()
    api = "rca-testbed-banking/testbed-api"
    commerce = {
        "domain": "commerce",
        "scoring": {"accept": ["commerce-order"]},
        "must_support": ["게이트웨이 POST /api/orders/** 서버 스팬 5xx 0.05 이상, core-banking 이체는 정상"],
        "description": "payment calls http://testbed-transfer:8082/api/transfers for the settlement.",
    }
    assert api not in excluded_targets(active, "X", commerce)
    named = {**commerce, "must_rule_out": ["banking api 의 이체 스팬은 평시와 같다"]}
    assert excluded_targets(active, "X", named)[api] == "scored_item"
    banking = {"domain": "core-banking", "scoring": {"accept": ["core-banking-ledger"]},
               "must_support": ["POST /api/transfers 5xx"]}
    assert excluded_targets(active, "X", banking)[api] == "scored_item"
    # A later segment names the resource, so the app behind it stays out.
    resource = {**banking, "description": "commerce checkout calls /api/payment/settle and fails"}
    assert excluded_targets(active, "X", resource)["rca-testbed-commerce/testbed-payment"] == "mentioned"


def test_a_short_word_names_another_domain_only_when_that_domain_is_named() -> None:
    """``order`` in a commerce answer key is commerce order; food order stays a
    candidate unless the answer key names the food domain. A banking key that
    names commerce (``commerce payment``) keeps commerce's short words out, and
    the Korean 게이트웨이 follows the same rule."""
    active = _three_domains()
    commerce = {
        "domain": "commerce",
        "scoring": {"accept": ["commerce-postgres"]},
        "must_support": ["order 의 Hikari 대기가 늘고 payment 가 실패한다"],
    }
    reasons = excluded_targets(active, "X", commerce)
    assert {"rca-testbed-commerce/testbed-order", "rca-testbed-commerce/testbed-payment"} <= set(reasons)
    assert "rca-testbed-food/testbed-order" not in reasons
    assert "rca-testbed-food/testbed-payment" not in reasons
    with_food = {**commerce, "description": "food delivery stays healthy"}
    assert "rca-testbed-food/testbed-order" in excluded_targets(active, "X", with_food)

    banking = {
        "domain": "core-banking",
        "scoring": {"accept": ["core-banking-ledger"]},
        "must_support": ["api 'Account service call failed for order null: 400' 이 늘어난다"],
        "description": "commerce payment also calls transfer directly.",
        "must_rule_out": ["게이트웨이와 각 서비스 서버 스팬에 429 가 없다"],
    }
    reasons = excluded_targets(active, "X", banking)
    assert reasons["rca-testbed-commerce/testbed-payment"] == "mentioned"
    # Ambiguous (commerce is named, "order" could be its app): stays out.
    assert reasons["rca-testbed-commerce/testbed-order"] == "scored_item"
    assert reasons["rca-testbed-commerce/testbed-gateway"] == "scored_item"
    assert "rca-testbed-food/testbed-order" not in reasons
    assert "rca-testbed-food/testbed-payment" not in reasons
    # A unique name (게이트웨이, gateway) is not ambiguous: it counts even when
    # nothing else names commerce (U7b review M1).
    unnamed = {**banking, "description": "the load generator calls transfer directly."}
    assert excluded_targets(active, "X", unnamed)["rca-testbed-commerce/testbed-gateway"] == "scored_item"


def _real_policy_or_three_domains():
    if (REGISTRY / "decoy-deployments.json").is_file():
        return load_decoy_policy(REGISTRY / "decoy-deployments.json")[0]
    return _three_domains()


@pytest.mark.parametrize(
    "description",
    [
        "Path: gateway -> order -> cart -> pricing -> product -> inventory -> payment -> transfer.",
        "정산 이체가 실패해 게이트웨이가 502 를 낸다.",
    ],
)
def test_a_path_in_an_unnamed_domain_written_with_unique_names_stays_on_the_path(description) -> None:
    """U7b review M1 (S1, S2): a bank answer key that never says commerce but
    lists commerce apps by their unique names (cart, pricing, gateway,
    게이트웨이). Those names count, name commerce, and so pull in the
    ambiguous ones (order, payment) of the same listing."""
    active = _real_policy_or_three_domains()
    scenario = {"domain": "core-banking", "scoring": {"accept": ["core-banking-ledger"]}, "description": description}
    reasons = excluded_targets(active, "X", scenario)
    commerce = {t.key for t in active.targets if t.namespace == "rca-testbed-commerce"}
    if "cart" in description:
        expected = {k for k in commerce if k.rsplit("testbed-", 1)[1] in description}
    else:
        expected = {"rca-testbed-commerce/testbed-gateway"}
    assert expected <= set(reasons), sorted(expected - set(reasons))
    keys = _candidate_keys(active, "X", scenario, "mentioned")
    assert not expected & keys
    forbidden, _ = _oracle_forbidden(active, scenario, "mentioned")
    assert expected <= forbidden, "the exhaustive oracle must see the same path on its own"


def test_a_policy_domain_without_domain_words_is_refused() -> None:
    """U7b review M2: a policy domain the module has no words for would be read
    as never named. The policy is refused with the reason instead."""
    with pytest.raises(ValueError, match="logistics.*no domain words"):
        policy(domain_namespaces={"commerce": "rca-testbed-commerce", "logistics": "rca-testbed-logistics"})


def test_no_change_sentences_stay_blunt_across_domains() -> None:
    """The domain boundary is for the path reading only. A no-change sentence
    still takes every short word in every namespace (review2 B4 side), and a
    URL prefix or another domain's word no longer switches off the "nothing
    else changed" fallback (U7 review3 M3)."""
    active = _three_domains()
    banking = {"domain": "core-banking", "must_rule_out": ["order 롤아웃이 없다"]}
    unchanged = set(stated_unchanged(active, banking))
    assert {"rca-testbed-commerce/testbed-order", "rca-testbed-food/testbed-order"} <= unchanged
    for key in ("rca-testbed-commerce/testbed-order", "rca-testbed-food/testbed-order"):
        assert excluded_targets(active, "X", banking)[key] == "stated_unchanged"
    generic = {"domain": "commerce", "description": "Nothing else changes: no rollout and no restart behind /api/checkout."}
    unchanged = set(stated_unchanged(active, generic))
    assert {t.key for t in active.targets if t.namespace == "rca-testbed-commerce"} <= unchanged
    assert "rca-testbed-banking/testbed-api" in unchanged


def test_a_scenario_without_a_domain_gets_no_decoy() -> None:
    """review2 M3: without a domain the bare words and the namespace fallback
    cannot be read, so no schedule is drawn rather than a wrong one."""
    for seed in range(50):
        assert schedule(seed, scenario={"description": "Apply an invalid image to the payment deployment"}) == []


def test_partition_requires_the_preflight_time() -> None:
    with pytest.raises(TypeError):
        partition_decoy_alarms([], [], grace_sec=900)  # type: ignore[call-arg]


REVIEW2_CASES = [
    # (scenario, scope, target that must not be a candidate)
    ("F68-R", "mentioned", "rca-testbed-commerce/testbed-order"),
    ("F40-H", "mentioned", "rca-testbed-commerce/testbed-gateway"),
    ("F01-P", "mentioned", "rca-testbed-commerce/testbed-gateway"),
    ("F01-P", "mentioned", "rca-testbed-commerce/testbed-inventory"),
    ("F48-P", "scored", "rca-testbed-food/testbed-restaurant"),
    ("F38-R", "scored", "rca-testbed-food/testbed-restaurant"),
    ("F50-R", "scored", "rca-testbed-banking/testbed-ledger"),
    ("F58-R", "scored", "rca-testbed-food/testbed-restaurant"),
    ("F57-P", "scored", "rca-testbed-commerce/testbed-gateway"),
    ("F66-R", "scored", "rca-testbed-commerce/testbed-gateway"),
]


@pytest.mark.parametrize("scenario_id,scope,key", REVIEW2_CASES)
def test_review2_reproductions_on_the_real_answer_keys(scenario_id, scope, key) -> None:
    active, scenarios = _real()
    assert key not in _candidate_keys(active, scenario_id, scenarios[scenario_id], scope)


# D1 review N2 / review2 R3: answer-key wording D1 introduced excluded these
# for a name the key never meant ("/api/orders" read as banking api, "for
# order null" read as food order). They are candidates again (U7b).
D1_FALSE_EXCLUSIONS = [
    *[(sid, "rca-testbed-banking/testbed-api") for sid in (
        "F01-R", "F01-H", "F08-H", "F11-R", "F05-R", "F05-H", "F06-H", "F05-P", "F15-T1",
        "F15-R", "F25-H", "F23-R", "F30-R", "F66-R", "F60-H", "F67-R", "F53-H", "F69-R",
    )],
    ("F17-P", "rca-testbed-food/testbed-order"),
]
# Kept out on purpose: F17-R names the commerce gateway's spans as evidence, and
# F17-P names commerce ("commerce payment"), so its "order" stays ambiguous.
D1_KEPT_EXCLUSIONS = [
    ("F17-R", "rca-testbed-commerce/testbed-gateway"),
    ("F17-P", "rca-testbed-commerce/testbed-order"),
]


@pytest.mark.parametrize("scope", ["mentioned", "scored"])
def test_d1_wording_no_longer_excludes_other_domains_by_a_stray_word(scope) -> None:
    active, scenarios = _real()
    returned = [
        (sid, key) for sid, key in D1_FALSE_EXCLUSIONS
        if key in _candidate_keys(active, sid, scenarios[sid], scope)
    ]
    assert returned == D1_FALSE_EXCLUSIONS
    for sid, key in D1_KEPT_EXCLUSIONS:
        assert key not in _candidate_keys(active, sid, scenarios[sid], scope), (sid, key)


# Independent, deliberately blunt oracle for the exhaustive check below: it does
# not reuse the module's matching, so a gap in one is not hidden by the other.
_CHANGE = re.compile(
    r"롤아웃|재배포|배포|변경|릴리스|재시작|재기동|ReplicaSet|rollout|redeploy|deploy|restart|release|change",
    re.IGNORECASE,
)
_NEG = re.compile(r"없|아니|아님|\bno\b|\bnot\b|without|nothing|untouched|unchanged", re.IGNORECASE)


def _oracle_names(target):
    short = target.deployment.removeprefix("testbed-")
    return [target.service_name, target.target_id, *target.aliases, target.deployment, target.container, short]


def _oracle_word(name, text):
    return re.search(rf"(?<![A-Za-z0-9-]){re.escape(name)}(?![A-Za-z0-9-])", text) is not None


def _oracle_scoped(active, text):
    """Product target names ``<namespace>/<object>``: the object they name
    (an app only when the object is its Deployment) and the text without them.
    Token based on purpose, unlike the module's regex (TRIG 2026-11-12)."""
    hits, kept = set(), []
    namespaces = {target.namespace for target in active.targets}
    for token in re.split(r"(\s+)", text):
        bare = token.strip("()[]{}'\"`,;")
        namespace, slash, obj = bare.partition("/")
        if slash and namespace in namespaces:
            obj = obj.split(":", 1)[0]
            if obj.startswith("deployment/"):
                obj = obj[len("deployment/"):]
            head = obj.split("/")[0]
            hits |= {
                t.key for t in active.targets
                if t.namespace == namespace and re.fullmatch(re.escape(t.deployment) + r"(-[a-z0-9]+)*", head)
            }
            kept.append(" ")
        else:
            kept.append(token)
    return hits, "".join(kept)


def _oracle_hits(active, text):
    hits, text = _oracle_scoped(active, text)
    for target in active.targets:
        if target.namespace in text or any(_oracle_word(name, text) for name in _oracle_names(target)):
            hits.add(target.key)
    return hits


# U7b: on the path reading an everyday short word of another domain is a name
# only when the answer key names that domain. The oracle keeps its own, shorter
# list of such words (the ones the reviews actually read as data: "in order",
# "application account", "/api/orders"); every other short name, and the
# Korean names below, count everywhere and name their domain (review M1). It
# says "named" on a wider vocabulary than the module (more words make it
# stricter), and it drops only the literal URL prefix "/api/".
_ORACLE_EVERYDAY = {"order", "payment", "api", "account", "ledger", "inventory", "dispatch"}
# 장바구니 is not here: answer keys use it for the data ("장바구니 품목마다",
# order's cart items), not for the cart app.
_ORACLE_KOREAN = {"gateway": "게이트웨이"}
_ORACLE_DOMAIN = {
    "rca-testbed-commerce": re.compile(r"commerce|커머스|checkout|체크아웃|상품|장바구니", re.IGNORECASE),
    "rca-testbed-banking": re.compile(r"banking|은행|뱅킹|oracle|이체|계좌", re.IGNORECASE),
    "rca-testbed-food": re.compile(r"food|배달|음식|restaurant|식당|배차|mysql", re.IGNORECASE),
}


def _oracle_strong(target):
    short = target.deployment.removeprefix("testbed-")
    names = [target.service_name, target.target_id, *target.aliases, target.deployment, target.container]
    return names if short in _ORACLE_EVERYDAY else [*names, short]


def _oracle_korean(target, text):
    korean = _ORACLE_KOREAN.get(target.deployment.removeprefix("testbed-"))
    return korean is not None and korean in text


def _oracle_named(active, whole):
    named = {ns for ns, words in _ORACLE_DOMAIN.items() if ns in whole or words.search(whole)}
    for target in active.targets:
        if any(_oracle_word(name, whole) for name in _oracle_strong(target)) or _oracle_korean(target, whole):
            named.add(target.namespace)
    return named


def _oracle_path_hits(active, text, home, named):
    hits, text = _oracle_scoped(active, text)
    unprefixed = text.replace("/api/", "/~/")
    for target in active.targets:
        short = target.deployment.removeprefix("testbed-")
        if (
            target.namespace in text
            or any(_oracle_word(name, text) for name in _oracle_strong(target))
            or _oracle_korean(target, text)
        ):
            hits.add(target.key)
        elif target.namespace == home or home is None:
            if _oracle_word(short, text):
                hits.add(target.key)
        elif target.namespace in named and _oracle_word(short, unprefixed):
            hits.add(target.key)
    return hits


def _oracle_forbidden(active, scenario, scope):
    def texts(node, top=None):
        if isinstance(node, str):
            yield top, node
        elif isinstance(node, dict):
            for key, value in node.items():
                if key != "related_scenarios":
                    yield from texts(value, top or key)
        elif isinstance(node, list):
            for value in node:
                yield from texts(value, top)

    forbidden = set()
    namespace = active.domain_namespaces.get(scenario.get("domain"))
    named = _oracle_named(active, "\n".join(text for _, text in texts(scenario)))
    for top, text in texts(scenario):
        if scope == "mentioned" or top in {"root_cause", "scoring", "must_support", "must_rule_out"}:
            forbidden |= _oracle_path_hits(active, text, namespace, named)
        # Circled numbers enumerate separate facts ("③ ... POST /api/orders 만
        # 500 ④ rca-testbed-food 에 롤아웃이 없고"); without this split a
        # route in one fact and a no-change word in the next read as one
        # sentence about banking api (U7b, after the path reading stopped
        # masking it).
        for sentence in re.split(r"(?<=[.!?。])\s+|\n+|;\s|[\u2460-\u2473]", text):
            if _CHANGE.search(sentence) and _NEG.search(sentence):
                hits = _oracle_hits(active, sentence)
                forbidden |= hits
    return forbidden, namespace


@pytest.mark.parametrize("scope", ["mentioned", "scored"])
def test_no_real_scenario_offers_a_path_or_unchanged_target(scope) -> None:
    """Every real answer key (135), both scopes: no candidate is on the fault
    path or stated unchanged, as judged by the blunt oracle above (no-change
    sentences: every short word in every namespace; path: short words held to
    the domain boundary on the oracle's own wider vocabulary, U7b)."""
    active, scenarios = _real()
    leaks = []
    for scenario_id, scenario in scenarios.items():
        forbidden, _ = _oracle_forbidden(active, scenario, scope)
        leaked = _candidate_keys(active, scenario_id, scenario, scope) & forbidden
        if leaked:
            leaks.append((scenario_id, sorted(leaked)))
    assert leaks == [], leaks[:10]


# --- TRIG 2026-11-12: product target names `<namespace>/<object>` ----------
def test_a_product_target_name_names_that_one_object_not_its_namespace() -> None:
    """The answer keys score external mocks by the product's target name
    (``rca-testbed-food/testbed-external-pg-mock``). Read as a namespace
    substring it took every app of the domain off the decoy list (TRIG review
    2026-11-12). It names that one object: an app Deployment when it is one,
    nothing in the policy otherwise. A bare namespace still names them all."""
    active = _three_domains()
    food = {t.key for t in active.targets if t.namespace == "rca-testbed-food"}
    assert mentioned_targets(active, "rca-testbed-food/testbed-external-pg-mock") == set()
    assert mentioned_targets(active, "rca-testbed-food/job/notify-delivery-backfill") == set()
    assert mentioned_targets(active, "rca-testbed-food/testbed-payment") == {"rca-testbed-food/testbed-payment"}
    assert mentioned_targets(active, "rca-testbed-commerce/deployment/testbed-order") == {
        "rca-testbed-commerce/testbed-order"
    }
    assert mentioned_targets(active, "every app in rca-testbed-food restarted") == food
    scenario = {
        "domain": "food-delivery",
        "root_cause": {"target_id": "rca-testbed-food/testbed-external-pg-mock"},
        "scoring": {"accept": ["rca-testbed-food/testbed-external-pg-mock"], "partial": ["food-delivery-order"]},
    }
    same = {t.key for t in decoy_candidates(active, "X", scenario)["same_domain"]}
    assert same == {"rca-testbed-food/testbed-payment"}


@pytest.mark.parametrize(
    ("scenario_id", "same_domain"),
    [
        ("F06-P", {"rca-testbed-food/testbed-dispatch", "rca-testbed-food/testbed-notify"}),
        ("F19-P", {"rca-testbed-food/testbed-notify"}),
        ("F19-S", {"rca-testbed-food/testbed-notify", "rca-testbed-food/testbed-restaurant"}),
    ],
)
def test_real_mock_scenarios_keep_their_same_domain_decoys(scenario_id, same_domain) -> None:
    """The food mock scenarios had these same-domain candidates while the
    answer key used the shared short mock name; renaming the mock to its
    product target name must not take them away."""
    active, scenarios = _real()
    groups = decoy_candidates(active, scenario_id, scenarios[scenario_id])
    assert {t.key for t in groups["same_domain"]} == same_domain


# Hand-written expectations (rv-trig 4th review): the oracle reads product
# target names with the same rule as the module, so the exhaustive check cannot
# catch a gap shared by both. These values are written out by hand.
FOOD_APPS = {
    "rca-testbed-food/testbed-order",
    "rca-testbed-food/testbed-restaurant",
    "rca-testbed-food/testbed-dispatch",
    "rca-testbed-food/testbed-payment",
    "rca-testbed-food/testbed-notify",
}


@pytest.mark.parametrize(
    "name",
    [
        "rca-testbed-food/testbed-payment",
        "rca-testbed-food/deployment/testbed-payment",
        "rca-testbed-food/testbed-payment-78b789d675",
        "rca-testbed-food/testbed-payment-78b789d675-22rrw",
        "rca-testbed-food/testbed-payment-78b789d675-22rrw/payment-service",
    ],
)
def test_a_pod_replicaset_or_container_target_name_names_its_app(name) -> None:
    """A pod, ReplicaSet or container is a product target too. Its app is on
    the fault path: the old reading took all five food apps off (namespace
    substring), the first object reading took none; it is exactly one."""
    active, _ = _real()
    assert {t.key for t in active.targets if t.namespace == "rca-testbed-food"} == FOOD_APPS
    assert mentioned_targets(active, name) == {"rca-testbed-food/testbed-payment"}
    scenario = {"domain": "food-delivery", "scoring": {"accept": ["food-delivery-dispatch"], "partial": [name]}}
    excluded = excluded_targets(active, "X", scenario)
    assert "rca-testbed-food/testbed-payment" in excluded
    same = {t.key for t in decoy_candidates(active, "X", scenario)["same_domain"]}
    assert same == FOOD_APPS - {"rca-testbed-food/testbed-payment", "rca-testbed-food/testbed-dispatch"}


def test_a_non_app_target_name_names_no_app_and_a_bare_namespace_names_all() -> None:
    active, _ = _real()
    for name in (
        "rca-testbed-food/testbed-external-pg-mock",
        "rca-testbed-food/job/notify-delivery-backfill",
        "rca-testbed-food/payment-audit-export",
        "rca-testbed-food/testbed-mysql-0",
    ):
        assert mentioned_targets(active, name) == set(), name
    assert mentioned_targets(active, "rca-testbed-food 의 앱이 모두 재시작") == FOOD_APPS


# --- VM testbed decoys (civil-petition, review rv-t4o M3, Q35 answer condition ②) --------------------------------

def _vm_target(node, *, kinds=("war_redeploy", "config_reapply"), service=None):
    return {
        # The namespace key must not be the domain value itself: answer keys carry their domain as text, and a
        # target whose namespace appears in the text is read as named (every VM target would be excluded).
        "namespace": "civil-petition-vm", "deployment": f"petition-{node}", "container": "petition",
        "service_name": service or f"civil-petition-{node}", "target_id": f"uuid-{node}", "rollout_ok": True,
        "aliases": [], "transport": "vm", "node": node, "kinds": list(kinds),
    }


def _vm_policy(**overrides) -> DecoyPolicy:
    document = policy_document()
    document["domain_namespaces"]["civil-petition"] = "civil-petition-vm"
    document["targets"] += [_vm_target("was01"), _vm_target("was02"), _vm_target("web01", kinds=("config_reapply",))]
    document["kind_weights"].update({"war_redeploy": 20, "config_reapply": 20})
    document["vm_hold_sec"] = {"min": 96, "max": 1878}  # the whole recorded range (command-session decision)
    document.update(overrides)
    return DecoyPolicy.model_validate(document)


VM_SCENARIO = {
    "domain": "civil-petition",
    "root_cause": {"target_id": "uuid-db01"},
    "scoring": {"accept": ["uuid-db01"], "partial": []},
    "description": "db01 데이터 디스크가 가득 찬다. was01 은 피해자다.",
}


def test_vm_targets_take_only_vm_kinds_and_kubernetes_targets_never_do() -> None:
    from app.decoys import VM_KINDS

    policy = _vm_policy()
    kinds_by_target: dict[str, set[str]] = {}
    for seed in range(400):
        for scenario in (VM_SCENARIO, SCENARIO):
            for record in build_schedule(policy, seed=seed, scenario_id="F99-X" if scenario is VM_SCENARIO else "F01-R",
                                         scenario=scenario, cycle_start=CYCLE_START, buffer_start=BUFFER_START):
                kinds_by_target.setdefault(f"{record.namespace}/{record.deployment}", set()).add(record.kind)
                assert (record.node is not None) == (record.kind in VM_KINDS)
    assert kinds_by_target["civil-petition-vm/petition-web01"] == {"config_reapply"}
    assert kinds_by_target["civil-petition-vm/petition-was02"] <= VM_KINDS
    assert all(not (kinds & VM_KINDS) for key, kinds in kinds_by_target.items() if not key.startswith("civil-petition-vm/"))
    # VM decoys exist, but only in this domain's own recordings (review rv-t4u R2 (a)).
    assert any(key.startswith("civil-petition-vm/") for key in kinds_by_target)
    with pytest.raises(ValueError, match="need vm_hold_sec"):
        _vm_policy(vm_hold_sec=None)
    with pytest.raises(ValueError, match="must not be below its min"):
        _vm_policy(vm_hold_sec={"min": 600, "max": 300})
    with pytest.raises(ValueError, match="VM decoy target needs a node"):
        _vm_policy(targets=[*policy_document()["targets"], {**_vm_target("db01"), "kinds": ["restart"]}])
    with pytest.raises(ValueError, match="belong to VM decoy targets only"):
        DecoyPolicy.model_validate({**policy_document(), "targets": [
            {**policy_document()["targets"][0], "node": "was01"}]})


def test_a_vm_decoy_stays_off_the_fault_path_like_any_decoy() -> None:
    """was01 is named in the answer key (a victim) and db01 is the cause: neither carries a decoy in this scenario;
    was02 and web01 may (same domain), Kubernetes apps may (other domains)."""
    policy = _vm_policy()
    excluded = excluded_targets(policy, "F99-X", VM_SCENARIO)
    assert "civil-petition-vm/petition-was01" in excluded
    groups = decoy_candidates(policy, "F99-X", VM_SCENARIO)
    assert {t.key for t in groups["same_domain"]} == {"civil-petition-vm/petition-was02", "civil-petition-vm/petition-web01"}
    assert groups["off_domain"] and all(t.transport == "kubernetes" for t in groups["off_domain"])
    # A VM target named by an alias in the answer key is off the path too (review rv-t4u L5: role names).
    document = _vm_policy().model_dump(by_alias=True)
    for target in document["targets"]:
        if target.get("node") == "was02":
            target["aliases"] = ["second WAS"]
    aliased = DecoyPolicy.model_validate(document)
    scenario = {**VM_SCENARIO, "description": VM_SCENARIO["description"] + " second WAS 도 느려진다."}
    assert "civil-petition-vm/petition-was02" in excluded_targets(aliased, "F99-X", scenario)


def test_vm_decoy_executor_applies_holds_then_undoes_like_an_injection() -> None:
    """Review rv-t4u R1 (a): two operator calls like an injection's run and cleanup, the revert after the drawn
    gap, not seconds later."""
    from datetime import datetime as _dt

    from app.decoys import CompositeDecoyExecutor, VmDecoyExecutor

    calls = []
    answers = []
    now = [_dt(2026, 11, 10, 1, 30, tzinfo=timezone.utc)]

    def run(argv, **kwargs):
        calls.append(argv[3:])
        return answers.pop(0) if answers else subprocess.CompletedProcess([], 0, "", "")

    vm = VmDecoyExecutor(process_runner=run, cli=Path("/opt/lucida/scenario-contracts/profiles/operator_session.py"),
                         clock=lambda: now[0])
    kube = Recorder()
    executor = CompositeDecoyExecutor(KubectlDecoyExecutor(_vm_policy(), process_runner=kube), vm)
    record = DecoyRecord(id="d1", window="normal", planned_at="2026-11-10T01:30:00Z", kind="war_redeploy",
                         namespace="civil-petition-vm", deployment="petition-was02", node="was02", gap_sec=420,
                         service_name="was02", target_id="uuid-was02", relation="same_domain", value="x",
                         started_at="2026-11-10T01:30:00Z")
    executor.apply(record)
    assert calls == [["was02", "--decoy-change", "war_redeploy"]]
    now[0] += timedelta(seconds=419)
    assert executor.rollout_done(record) is False and len(calls) == 1  # still holding: no session
    now[0] += timedelta(seconds=1)
    assert executor.rollout_done(record) is True
    assert calls[-1] == ["was02", "--decoy-revert", "war_redeploy"]
    assert kube.calls == []
    # The queue waits the gap plus the undo call before it gives up.
    # Decoy calls have their own limit (CLI limit + 60s), longer than the routine check's 150s.
    from app.decoys import VM_DECOY_CALL_TIMEOUT_SEC, VM_DECOY_CLI_LIMIT_SEC

    assert VM_DECOY_CALL_TIMEOUT_SEC == VM_DECOY_CLI_LIMIT_SEC + 60 > 150
    assert executor.timeout_sec(record, 420) == 420 + 2 * VM_DECOY_CALL_TIMEOUT_SEC
    assert executor.timeout_sec(_record(), 420) == 420
    executor.undo(record)
    assert calls[-1] == ["was02", "--decoy-revert", "war_redeploy"]
    ok = subprocess.CompletedProcess([], 0, "", "")
    answers[:] = [subprocess.CompletedProcess([], 1, "", "petition health did not come back in 60s\n"), ok,
                  subprocess.CompletedProcess([], 4, "", "the executor's run ended with 1\n"), ok,
                  subprocess.CompletedProcess([], 75, "", "operator lock on was02 is held\n"),
                  subprocess.CompletedProcess([], 124, "", "session limit 90s\n")]
    # Each change failure is recorded by its cause (review rv-t4u2 N5) and followed by one revert (N4).
    with pytest.raises(RuntimeError, match=r"change on was02 not healthy, reverted by the CLI \(exit 1\): petition "
                                           r"health.*; reverted again after it"):
        executor.apply(record)
    assert calls[-1] == ["was02", "--decoy-revert", "war_redeploy"]
    with pytest.raises(RuntimeError, match=r"executor failed and reverted as root \(exit 4\)"):
        executor.apply(record)
    # 75: nothing was changed, so no revert follows (review rv-t4u3 L1); the queue records the decoy as skipped.
    from app.decoys import VmDecoyBusy

    before = len(calls)
    with pytest.raises(VmDecoyBusy, match=r"change on was02 busy \(exit 75\): operator lock on was02 is held"):
        executor.apply(record)
    assert len(calls) == before + 1 and calls[-1] == ["was02", "--decoy-change", "war_redeploy"]
    with pytest.raises(RuntimeError, match=r"revert on was02 over the CLI limit \(exit 124\): session limit 90s"):
        executor.undo(record)
    executor.apply(_record())  # a Kubernetes record still goes to kubectl
    assert kube.calls


def _recorded_quantile(q: float) -> float:
    from app.decoys import VM_DECOY_RECORDED_GAPS_SEC as values

    position = q * (len(values) - 1)
    index = min(int(position), len(values) - 2)
    return values[index] + (values[index + 1] - values[index]) * (position - index)


def test_vm_decoy_gaps_follow_the_injection_lengths_and_end_before_the_buffer() -> None:
    import random

    from app.decoys import VM_DECOY_CALL_ROOM_SEC, VM_DECOY_RECORDED_GAPS_SEC, draw_vm_decoy_gap

    assert len(VM_DECOY_RECORDED_GAPS_SEC) == 129 and list(VM_DECOY_RECORDED_GAPS_SEC) == sorted(VM_DECOY_RECORDED_GAPS_SEC)
    rng = random.Random(5)
    gaps = sorted(draw_vm_decoy_gap(rng) for _ in range(5000))
    assert VM_DECOY_RECORDED_GAPS_SEC[0] - 20 <= gaps[0] and gaps[-1] <= VM_DECOY_RECORDED_GAPS_SEC[-1] + 20
    # Each decile of the draws sits near the recorded one (within 10%).
    for decile in range(1, 10):
        drawn, recorded = gaps[decile * len(gaps) // 10], _recorded_quantile(decile / 10)
        assert abs(drawn - recorded) <= 0.10 * recorded, decile
    for seed in range(400):
        for record in build_schedule(_vm_policy(), seed=seed, scenario_id="F99-X", scenario=VM_SCENARIO,
                                     cycle_start=CYCLE_START, buffer_start=BUFFER_START):
            if record.node is None:
                assert record.gap_sec is None
                continue
            if record.revert == "after_recording":
                # Put back after the recording (D1 (b)): no gap, a guard that outlasts the recording.
                planned = datetime.strptime(record.planned_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                assert record.gap_sec is None
                assert record.guard_sec == int((BUFFER_START - planned).total_seconds()) + 7800
                continue
            if record.status == "skipped":
                # No recorded length fit before the buffer: not run, and the record says why.
                assert record.gap_sec is None and "fits before the buffer" in (record.detail or "")
                continue
            planned = datetime.strptime(record.planned_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            assert 96 <= record.gap_sec <= 1878
            assert planned + timedelta(seconds=record.gap_sec + VM_DECOY_CALL_ROOM_SEC) <= BUFFER_START


def test_a_vm_decoy_with_no_gap_that_fits_is_skipped_and_recorded(monkeypatch) -> None:
    """Command-session decision: when no recorded length fits before the buffer even after drawing again, the
    decoy is not run and the record says why (nothing is cut to fit)."""
    import app.decoys as decoys

    monkeypatch.setattr(decoys, "draw_vm_decoy_gap_within", lambda rng, low, high: None)
    records = [r for seed in range(200) for r in build_schedule(
        _vm_policy(), seed=seed, scenario_id="F99-X", scenario=VM_SCENARIO, cycle_start=CYCLE_START,
        buffer_start=BUFFER_START) if r.node and r.revert == "before_fault"]
    assert records and all(r.status == "skipped" and r.gap_sec is None and "fits before the buffer" in r.detail for r in records)


def test_a_vm_decoy_alarm_is_excused_by_its_service_or_target_name() -> None:
    record = DecoyRecord(id="d1", window="normal", planned_at="2026-11-10T02:30:00Z", kind="config_reapply",
                         namespace="civil-petition-vm", deployment="petition-web01", node="web01",
                         service_name="civil-petition-web01", target_id="uuid-web01", relation="same_domain",
                         value="x", status="applied", started_at="2026-11-10T02:30:00Z",
                         finished_at="2026-11-10T02:32:00Z")
    remaining, excused = _partition([_alert({"target_id": "uuid-web01"}), _alert({"target_id": "uuid-db01"})],
                                    decoys=(record,))
    assert [a["labels"]["target_id"] for a in remaining] == ["uuid-db01"]
    assert len(excused) == 1


def test_a_kubernetes_decoy_keeps_its_recorded_shape_and_a_vm_decoy_names_its_node() -> None:
    from app.decoys import decoy_document

    vm = DecoyRecord(id="d2", window="normal", planned_at="2026-11-10T01:40:00Z", kind="war_redeploy",
                     namespace="civil-petition-vm", deployment="petition-was02", node="was02", gap_sec=600,
                     service_name="civil-petition-was02", target_id="uuid-was02", relation="off_domain", value="x")
    document = decoy_document(scenario_id="F01-R", seed=1, policy_sha256="a", cycle_start="2026-11-10T01:00:00Z",
                              records=[_record(), vm])
    assert set(document["decoys"][0]) == {
        "id", "window", "planned_at", "kind", "namespace", "deployment", "service_name", "target_id", "relation",
        "configmap", "value", "status", "started_at", "finished_at", "detail",
    }
    assert document["decoys"][1]["node"] == "was02" and document["decoys"][1]["gap_sec"] == 600


def _without_vm(policy: DecoyPolicy) -> DecoyPolicy:
    document = policy.model_dump(by_alias=True)
    document["targets"] = [t for t in document["targets"] if t.get("transport") != "vm"]
    document["kind_weights"] = {k: v for k, v in document["kind_weights"].items() if k not in ("war_redeploy", "config_reapply")}
    document["domain_namespaces"] = {k: v for k, v in document["domain_namespaces"].items() if k != "civil-petition"}
    return DecoyPolicy.model_validate(document)


def _kubernetes_scenarios_keep_their_decoys(policy: DecoyPolicy, scenarios: dict, seeds: range) -> int:
    """Review rv-t4u R2 (a): every Kubernetes scenario draws the same decoys, seed for seed, as with no VM targets
    in the policy at all, and never a VM decoy. Returns how many decoys were compared."""
    plain = _without_vm(policy)
    compared = 0
    for scenario_id, scenario in scenarios.items():
        if scenario.get("domain") == "civil-petition":
            continue
        for seed in seeds:
            kwargs = dict(seed=seed, scenario_id=scenario_id, scenario=scenario, cycle_start=CYCLE_START,
                          buffer_start=BUFFER_START)
            with_vm = build_schedule(policy, **kwargs)
            assert all(record.node is None for record in with_vm), scenario_id
            assert with_vm == build_schedule(plain, **kwargs), (scenario_id, seed)
            compared += len(with_vm)
    return compared


def test_vm_targets_never_change_a_kubernetes_scenarios_decoys() -> None:
    assert _kubernetes_scenarios_keep_their_decoys(_vm_policy(), {"F01-R": SCENARIO}, range(300)) > 100
    # And a civil-petition scenario does get VM decoys (same domain).
    vm = [r for seed in range(300) for r in build_schedule(_vm_policy(), seed=seed, scenario_id="F99-X",
          scenario=VM_SCENARIO, cycle_start=CYCLE_START, buffer_start=BUFFER_START) if r.node]
    assert vm


def test_the_real_policy_keeps_every_kubernetes_scenarios_decoys() -> None:
    root = REGISTRY
    if not (root / "decoy-deployments.json").is_file():
        pytest.skip("testbed-services registry is not checked out next to the runner")
    active, _ = load_decoy_policy(root / "decoy-deployments.json")
    scenarios = json.loads((root / "scenario-metadata.json").read_text(encoding="utf-8"))["scenarios"]
    if "civil-petition" not in active.domain_namespaces:
        active = _vm_policy()  # this checkout's policy has no VM targets yet: compare with the test policy
    assert _kubernetes_scenarios_keep_their_decoys(active, scenarios, range(3)) > 0


def test_a_failed_vm_change_is_reverted_once_and_a_failed_revert_is_unsettled() -> None:
    """Review rv-t4u2 N4: a change that ends in anything but 0 or 3 may have left something (a revert cut by the
    runner's limit, 124 in the middle of the run, a lock wait), so the revert is called once more. If it fails too,
    the decoy is unsettled. A refusal (3) touched nothing and is not followed by a revert."""
    from app.decoys import VmDecoyExecutor, VmDecoyLeftChanged

    calls, answers = [], []

    def run(argv, **kwargs):
        calls.append(argv[4])
        return answers.pop(0)

    vm = VmDecoyExecutor(process_runner=run)
    record = DecoyRecord(id="d1", window="normal", planned_at="2026-11-10T01:30:00Z", kind="config_reapply",
                         namespace="civil-petition-vm", deployment="petition-web01", node="web01", gap_sec=300,
                         service_name="web01", target_id="uuid-web01", relation="same_domain", value="x")
    answers[:] = [subprocess.CompletedProcess([], 124, "", "limit\n"), subprocess.CompletedProcess([], 1, "", "lock\n")]
    with pytest.raises(VmDecoyLeftChanged, match="revert after it also failed"):
        vm.apply(record)
    assert calls == ["--decoy-change", "--decoy-revert"]
    calls.clear()
    answers[:] = [subprocess.CompletedProcess([], 3, "", "web01 does not take war_redeploy\n")]
    with pytest.raises(RuntimeError, match=r"refused \(exit 3\)") as raised:
        vm.apply(record)
    assert calls == ["--decoy-change"] and not isinstance(raised.value, VmDecoyLeftChanged)


def test_vm_decoy_gaps_are_drawn_again_never_piled_on_an_edge() -> None:
    """Review rv-t4u2 N6 (b) and the command-session decision: nothing is cut to an edge (the old cut piled 8% on
    exactly 240 s and 4% on exactly 1500 s). With the policy range the whole recorded range, the draws keep the
    recorded shape: under 240 s about 4.7%, over 1500 s about 2.3%. A tighter range (the room before the buffer) is
    met by drawing again."""
    import random
    from collections import Counter

    from app.decoys import draw_vm_decoy_gap_within

    for low, high in ((96, 1878), (96, 900)):
        rng = random.Random(9)
        gaps = sorted(draw_vm_decoy_gap_within(rng, low, high) for _ in range(2000))
        assert low <= gaps[0] and gaps[-1] <= high
        assert Counter(gaps).most_common(1)[0][1] <= 20  # no value holds even 1% of the draws
        assert gaps.count(low) <= 5 and gaps.count(high) <= 5
    rng = random.Random(9)
    gaps = [draw_vm_decoy_gap_within(rng, 96, 1878) for _ in range(2000)]
    under, over = sum(g < 240 for g in gaps) / 2000, sum(g > 1500 for g in gaps) / 2000
    assert 0.02 <= under <= 0.08 and 0.008 <= over <= 0.04, (under, over)
    assert draw_vm_decoy_gap_within(random.Random(1), 300, 200) is None


def test_the_recorded_gaps_are_the_testbed_services_list_and_its_policy_range() -> None:
    """The runner draws from the same 129 recorded injection lengths testbed-services keeps, and the policy range
    is that list's two ends (command-session decision; the guard is the max + 600 seconds)."""
    from app.decoys import VM_DECOY_CLI_LIMIT_SEC, VM_DECOY_RECORDED_GAPS_SEC

    recorded = REGISTRY / "recorded-injection-durations.json"
    if not recorded.is_file():
        pytest.skip("this testbed-services checkout has no recorded-injection-durations.json yet")
    assert tuple(json.loads(recorded.read_text(encoding="utf-8"))["seconds"]) == VM_DECOY_RECORDED_GAPS_SEC
    active, _ = load_decoy_policy(REGISTRY / "decoy-deployments.json")
    assert active.vm_hold_sec is not None
    assert (active.vm_hold_sec.min, active.vm_hold_sec.max) == (VM_DECOY_RECORDED_GAPS_SEC[0], VM_DECOY_RECORDED_GAPS_SEC[-1])
    source = (REGISTRY.parent / "profiles" / "operator_session.py").read_text(encoding="utf-8")
    assert f"DECOY_LIMIT_SECONDS = {VM_DECOY_CLI_LIMIT_SEC}" in source


def test_near_fault_vm_decoys_move_earlier_instead_of_being_dropped() -> None:
    """Command-session decisions on the T4U-3 open question and on review rv-t4u3 D1, L4. A VM decoy put back before
    the fault draws its gap first and starts earlier when its revert would not end 600 s before the buffer, at a
    start drawn evenly over the starts that fit (never pinned to the latest allowed second); half the VM decoys are
    put back after the recording instead. Near-fault VM decoys then run about as often as near-fault Kubernetes
    decoys, so «an operator change near the fault» and «a change not reverted at the fault» are no simple rules."""
    from collections import Counter

    from app.decoys import VM_DECOY_CALL_ROOM_SEC

    policy = _vm_policy()
    vm, vm_skipped, kubernetes, revert = Counter(), Counter(), Counter(), Counter()
    pinned = before_fault = 0
    for seed in range(1500):
        records = build_schedule(policy, seed=seed, scenario_id="F99-X", scenario=VM_SCENARIO,
                                 cycle_start=CYCLE_START, buffer_start=BUFFER_START)
        assert [r.planned_at for r in records] == sorted(r.planned_at for r in records)  # the queue runs in order
        starts = [datetime.strptime(r.planned_at, "%Y-%m-%dT%H:%M:%SZ") for r in records if r.status != "skipped"]
        assert all((b - a).total_seconds() >= 720 for a, b in zip(starts, starts[1:]))  # a move never crowds
        for record in records:
            if record.node is None:
                continue
            if record.status == "skipped":
                vm_skipped[record.window] += 1
                continue
            vm[record.window] += 1
            revert[(record.kind, record.revert)] += 1
            if record.revert != "before_fault":
                continue
            before_fault += 1
            planned = datetime.strptime(record.planned_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            end = planned + timedelta(seconds=record.gap_sec + VM_DECOY_CALL_ROOM_SEC)
            assert end <= BUFFER_START
            pinned += end == BUFFER_START
        for record in build_schedule(policy, seed=seed, scenario_id="F01-R", scenario=SCENARIO,
                                     cycle_start=CYCLE_START, buffer_start=BUFFER_START):
            kubernetes[record.window] += 1
    share = lambda counts: counts["near_fault"] / sum(counts.values())  # noqa: E731
    assert share(vm) >= 0.85 * share(kubernetes), (share(vm), share(kubernetes))
    assert vm_skipped["near_fault"] <= 0.10 * (vm["near_fault"] + vm_skipped["near_fault"])
    # Half and half for the WAR variant (D1), never for a configuration value (D-A (a)), and the reverts are not piled
    # on the latest allowed second (L4).
    war = revert[("war_redeploy", "after_recording")] + revert[("war_redeploy", "before_fault")]
    assert war and 0.4 <= revert[("war_redeploy", "after_recording")] / war <= 0.6
    assert revert[("config_reapply", "before_fault")] > 0 and revert[("config_reapply", "after_recording")] == 0
    assert pinned <= 0.01 * before_fault


def test_a_vm_decoy_moved_before_another_takes_its_place_by_time(monkeypatch) -> None:
    """The queue runs planned decoys in list order, so a VM decoy moved earlier than a decoy drawn before it must
    come first in the list (long gaps make that happen)."""
    import app.decoys as decoys

    monkeypatch.setattr(decoys, "draw_vm_decoy_gap_within", lambda rng, low, high: min(high, 1878))
    reordered = 0
    for seed in range(600):
        records = build_schedule(_vm_policy(), seed=seed, scenario_id="F99-X", scenario=VM_SCENARIO,
                                 cycle_start=CYCLE_START, buffer_start=BUFFER_START)
        assert [r.planned_at for r in records] == sorted(r.planned_at for r in records)
        reordered += [r.id for r in records] != sorted((r.id for r in records), key=lambda i: int(i[1:]))
    assert reordered > 0


def test_a_held_vm_decoy_excuses_its_changes_alarm_and_only_on_its_target() -> None:
    """D1 (b): a VM decoy held through the recording explains an alarm of its change (the service restart) like a
    finished one, its finish being the end of the change; other states and other targets do not."""
    held = APPLIED.model_copy(update={"kind": "config_reapply", "status": "held", "node": "web01",
                                      "revert": "after_recording"})
    alert = _alert({"service_name": "commerce-order"})
    assert [item["decoy_id"] for item in _partition([alert], [held])[1]] == ["d1"]
    for status in ("planned", "rolling", "failed", "skipped"):
        assert _partition([alert], [held.model_copy(update={"status": status})])[1] == [], status
    assert _partition([_alert({"service_name": "commerce-payment"})], [held])[1] == []
    late = datetime(2026, 11, 10, 2, 48, tzinfo=timezone.utc)
    assert _partition([alert], [held], now=late)[1] == []


def test_held_excuses_count_each_held_vm_decoy_on_its_node() -> None:
    from app.decoys import held_excuses

    def vm(id_, node, status="held", paths=None):
        return APPLIED.model_copy(update={"id": id_, "kind": "config_reapply", "status": status, "node": node,
                                          "changed_paths": paths})
    records = [vm("d1", "web01", paths=["/a"]), vm("d2", "web01", paths=["/b"]), vm("d3", "was01"),
               vm("d4", "was02", status="applied", paths=["/c"]), APPLIED.model_copy(update={"status": "held"})]
    assert held_excuses(records) == {
        "web01": {"paths": {"/a", "/b"}, "guards": 2, "ids": ["d1", "d2"]},
        "was01": {"paths": set(), "guards": 1, "ids": ["d3"]},
    }


def test_vm_decoy_executor_reads_the_changed_paths_and_passes_the_guard() -> None:
    from app.decoys import DecoyRecord, VmDecoyExecutor

    calls = []
    out = ("health ok\n@@ decoy-changed /etc/apache2/sites-available/petition.conf\n"
           "@@ decoy-changed /etc/apache2/conf-available/x.conf\n@@ decoy-changed \n")
    vm = VmDecoyExecutor(process_runner=lambda argv, **k: calls.append(argv[3:]) or subprocess.CompletedProcess(argv, 0, out, ""))
    record = DecoyRecord(id="d1", window="normal", planned_at="2026-11-10T01:30:00Z", kind="config_reapply",
                         namespace="civil-petition-vm", deployment="petition-web01", node="web01",
                         service_name="web01", target_id="uuid-web01", relation="same_domain", value="x",
                         revert="after_recording", guard_sec=12000)
    assert vm.apply(record) == ["/etc/apache2/sites-available/petition.conf", "/etc/apache2/conf-available/x.conf"]
    vm.undo(record)
    assert calls == [["web01", "--decoy-change", "config_reapply", "--guard-sec", "12000"],
                     ["web01", "--decoy-revert", "config_reapply"]]
    before = record.model_copy(update={"revert": "before_fault", "guard_sec": None, "gap_sec": 600})
    calls.clear()
    vm.apply(before)
    assert calls == [["web01", "--decoy-change", "config_reapply"]]


def test_a_hold_longer_than_the_near_fault_window_allows_is_refused() -> None:
    """Review rv-t4u3 L8 (D4): a near-fault VM decoy moved earlier stays in its window only while the longest hold
    plus the revert room fits in it; a policy recounted past that is refused, not run with decoys leaving the window."""
    document = _vm_policy().model_dump(by_alias=True, exclude_none=True)
    near = document["windows"]["near_fault"]["start_before_buffer_sec"]
    document["vm_hold_sec"] = {"min": 96, "max": near - 600}
    assert DecoyPolicy.model_validate(document).vm_hold_sec.max == near - 600
    document["vm_hold_sec"] = {"min": 96, "max": near - 599}
    with pytest.raises(ValueError, match="must not exceed the near_fault window start"):
        DecoyPolicy.model_validate(document)
