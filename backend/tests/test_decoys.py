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
    # P4 not confirmed yet: the shipped policy rolls pods only by restart.
    assert {kind for kind, weight in active.kind_weights.items() if weight > 0} == {"restart"}
    for scenario_id, scenario in scenarios.items():
        excluded = excluded_targets(active, scenario_id, scenario)
        for seed in range(3):
            for record in build_schedule(
                active, seed=seed, scenario_id=scenario_id, scenario=scenario,
                cycle_start=CYCLE_START, buffer_start=BUFFER_START,
            ):
                assert record.kind == "restart"
                assert f"{record.namespace}/{record.deployment}" not in excluded, scenario_id


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


def _oracle_hits(active, text):
    hits = set()
    for target in active.targets:
        if target.namespace in text or any(
            re.search(rf"(?<![A-Za-z0-9-]){re.escape(name)}(?![A-Za-z0-9-])", text)
            for name in _oracle_names(target)
        ):
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
    for top, text in texts(scenario):
        if scope == "mentioned" or top in {"root_cause", "scoring", "must_support", "must_rule_out"}:
            forbidden |= _oracle_hits(active, text)
        for sentence in re.split(r"(?<=[.!?。])\s+|\n+|;\s", text):
            if _CHANGE.search(sentence) and _NEG.search(sentence):
                hits = _oracle_hits(active, sentence)
                forbidden |= hits
    return forbidden, namespace


@pytest.mark.parametrize("scope", ["mentioned", "scored"])
def test_no_real_scenario_offers_a_path_or_unchanged_target(scope) -> None:
    """Every real answer key (135), both scopes: no candidate is on the fault
    path or stated unchanged, as judged by the blunt oracle above."""
    active, scenarios = _real()
    leaks = []
    for scenario_id, scenario in scenarios.items():
        forbidden, _ = _oracle_forbidden(active, scenario, scope)
        leaked = _candidate_keys(active, scenario_id, scenario, scope) & forbidden
        if leaked:
            leaks.append((scenario_id, sorted(leaked)))
    assert leaks == [], leaks[:10]
