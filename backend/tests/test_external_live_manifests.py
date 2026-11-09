from __future__ import annotations

import json
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import pytest

from app.adaptive_runtime import SessionStatus
from app.coordinator import GlobalCoordinator
from app.manifests import ScenarioManifest
from app.production_runtime import RunArtifactStore
from app.production_runtime import production_runtime
from app.runner import ScenarioRunner
from app.main import api_run


def metadata_registry(tmp_path, scenario_id: str):
    path = tmp_path / "scenario-metadata.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "scenarios": {
                    scenario_id: {
                        "title": "Scenario title",
                        "description": "AI-authored scenario description.",
                        "cause": "Scenario cause",
                        "injection_summary": "Scenario injection summary.",
                        "user_impact": "Scenario user impact.",
                        "distinguishing_evidence": "Scenario distinguishing evidence.",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def runtime_contract(mode: str) -> dict:
    level = {
        "id": "fixed" if mode == "evaluation" else "low",
        "parameters": {"target_rps": 60},
        "min_hold": "1s",
        "timeout": "10s",
    }
    return {
        "mode": mode,
        "tick_interval": "1s",
        "max_injection_duration": "10s",
        "profile": {
            "kind": "fixed" if mode == "evaluation" else "adaptive_ladder",
            "levels": [level],
        },
        "success": {"all": [{"observation": "achieved", "op": "gte", "value": 60}]},
        "abort": {"all": [{"observation": "entry", "op": "ne", "value": 200}]},
        "must_rule_out": {"all": [{"observation": "other", "op": "gt", "value": 0}]},
        "preflight": ["canonical-kubeconfig", "global-dirty-lease"],
        "baseline": {"clean_window": "30m", "required": ["no-overlap"]},
        "observations": [
            {"id": "achieved", "adapter": "loadgen_summary", "query_id": "loadgen.achieved_rps", "freshness": "30s"},
            {"id": "entry", "adapter": "http_probe", "query_id": "http.entry_health", "freshness": "15s"},
            {"id": "other", "adapter": "database", "query_id": "database.tagged_session_count", "freshness": "30s"},
        ],
        "cleanup": {"required": True, "order": "reverse"},
        "recovery": {
            "all": [{"observation": "entry", "op": "eq", "value": 200}],
            "timeout": "10s",
        },
        "capture": {"enabled": False, "pre_window": "10m", "post_window": "20m", "model_snapshot": "/var/lib/lucida/ai-models/stream-anomaly/global/v1/model.json", "create_golden_anomaly": False},
    }


def manifest(mode: str, *, live: bool = True) -> ScenarioManifest:
    return ScenarioManifest.model_validate(
        {
            "schema_version": "1.0",
            "id": "F07-H" if mode == "calibration" else "F07-E",
            "slug": "f07-h-adaptive" if mode == "calibration" else "f07-e-fixed",
            "readiness": "ready",
            "injection": {"location": "tb-runner", "profile_refs": ["injector-profiles/load.north_south"]},
            "execution": {
                "preflight_ids": ["canonical-kubeconfig"],
                "controller": {
                    "live_enabled": live,
                    **(
                        {
                            "runtime": runtime_contract(mode),
                            "binding": {
                                "primary_ref": "load.north_south",
                                "companion_refs": [],
                                **(
                                    {"approved_profile_id": "F07-E.fixed.v1"}
                                    if mode == "evaluation"
                                    else {}
                                ),
                            },
                        }
                        if live
                        else {}
                    ),
                },
            },
            "actions": {
                action: {"mode": "dry-run", "mutation": False, "adapter_refs": ["load.north_south"]}
                for action in ("plan", "run", "cleanup")
            },
            "capture_policy": {"create_golden_anomaly": False},
        }
    )


class TerminalRuntime:
    def __init__(self, spec) -> None:
        self.session = SimpleNamespace(
            status=SessionStatus.CLEAN,
            spec=spec,
            t1=None,
            t2=None,
            model_dump=lambda **_: {"status": "clean"},
        )

    async def begin(self):
        return self.session


@pytest.mark.parametrize("mode", ["calibration", "evaluation"])
async def test_external_fixed_and_adaptive_manifests_reach_controller_runtime(
    tmp_path, monkeypatch, mode
) -> None:
    selected = manifest(mode)
    monkeypatch.setattr("app.runner.get_scenario", lambda _: None)
    monkeypatch.setattr("app.runner.get_manifest", lambda _: selected)
    seen = []

    def factory(**kwargs):
        seen.append(kwargs["scenario"])
        return TerminalRuntime(kwargs["scenario"].controller)

    runner = ScenarioRunner(
        tmp_path,
        tmp_path / "logs",
        coordinator=GlobalCoordinator(tmp_path / "coordinator.json"),
        runtime_factory=factory,
        artifact_store=RunArtifactStore(tmp_path / "runs"),
        dispatcher_path=tmp_path / "trusted-run-scenario.sh",
        scenario_metadata_path=metadata_registry(tmp_path, selected.id),
    )
    await runner.start(selected.id, "run")
    assert runner._task is not None
    await runner._task

    assert seen[0].id == selected.id
    assert seen[0].injection["catalog_slug"] == selected.slug
    current = runner.get_current()
    assert current.status == "succeeded"
    # The status API tail and the on-disk log carry the same controller lines.
    log_lines = runner.log_path(current.run_id).read_text(encoding="utf-8").splitlines()
    assert current.log_tail == log_lines
    assert current.log_tail[0].startswith(f"[BEGIN] run={current.run_id} ")
    assert current.log_tail[-1].startswith("[END] status=clean")


async def test_plan_only_external_manifest_is_refused_before_lease(tmp_path, monkeypatch) -> None:
    selected = manifest("calibration", live=False)
    monkeypatch.setattr("app.runner.get_scenario", lambda _: None)
    monkeypatch.setattr("app.runner.get_manifest", lambda _: selected)
    coordinator = GlobalCoordinator(tmp_path / "coordinator.json")
    runner = ScenarioRunner(tmp_path, tmp_path / "logs", coordinator=coordinator)

    with pytest.raises(RuntimeError, match="plan-only or unresolved"):
        await runner.start(selected.id, "run")
    assert coordinator.snapshot().active_lease is None


async def test_external_live_manifest_is_reachable_through_run_api(tmp_path, monkeypatch) -> None:
    selected = manifest("calibration")
    monkeypatch.setattr("app.runner.get_scenario", lambda _: None)
    monkeypatch.setattr("app.runner.get_manifest", lambda _: selected)
    runner = ScenarioRunner(
        tmp_path,
        tmp_path / "logs",
        coordinator=GlobalCoordinator(tmp_path / "coordinator.json"),
        runtime_factory=lambda **kwargs: TerminalRuntime(kwargs["scenario"].controller),
        artifact_store=RunArtifactStore(tmp_path / "runs"),
        dispatcher_path=tmp_path / "trusted-run-scenario.sh",
        scenario_metadata_path=metadata_registry(tmp_path, selected.id),
    )
    monkeypatch.setattr("app.main.get_runner", lambda: runner)
    response = await api_run(selected.slug)
    assert response.scenario_id == selected.id
    assert response.status == "running"
    assert runner._task is not None
    await runner._task


def test_production_runtime_uses_injected_live_probe_directly(tmp_path) -> None:
    scenario = manifest("calibration").runtime_scenario()
    assert scenario is not None

    class Probes:
        def inspect(self, request):
            raise AssertionError("creation must not probe")

        def observe(self, query):
            raise AssertionError("creation must not observe")

    probes = Probes()
    runtime = production_runtime(
        scenario=scenario,
        run_id="run-live-probe",
        fencing_token=1,
        clock=SimpleNamespace(now=lambda: datetime.now(timezone.utc)),
        evidence_path=tmp_path / "unused-evidence.json",
        observation_path=tmp_path / "unused-observations.json",
        live_probes=probes,  # type: ignore[arg-type]
    )
    assert runtime.eligibility_probe.probes is probes


@pytest.mark.parametrize("cleanup_succeeds", [True, False])
async def test_external_dirty_cleanup_clears_only_after_profile_recovery(
    tmp_path, monkeypatch, cleanup_succeeds
) -> None:
    selected = manifest("evaluation")
    monkeypatch.setattr("app.runner.get_scenario", lambda _: None)
    monkeypatch.setattr("app.runner.get_manifest", lambda _: selected)

    class Applier:
        def __init__(self, *args, **kwargs):
            pass

        def cleanup(self, request):
            return SimpleNamespace(
                succeeded=cleanup_succeeds,
                reason=None if cleanup_succeeds else "recovery failed",
            )

    monkeypatch.setattr("app.runner.TrustedDispatcherApplier", Applier)
    coordinator = GlobalCoordinator(tmp_path / "coordinator.json")
    runner_time = datetime.now(timezone.utc)
    lease = coordinator.acquire(
        run_id="dirty-run",
        scenario_id=selected.id,
        now=runner_time,
        lease_sec=30,
    )
    coordinator.mark_dirty(
        run_id=lease.run_id,
        fencing_token=lease.fencing_token,
        reason="initial cleanup failed",
        now=runner_time,
    )
    store = RunArtifactStore(tmp_path / "runs")
    contract = tmp_path / "contracts"
    contract.mkdir()
    plan = {
        "scenario": {"id": selected.id, "slug": selected.slug},
        "live_allowed": True,
        "plan_digest": "a" * 64,
        "profile_instances": [],
    }
    dispatcher = contract / "run-scenario.sh"
    dispatcher.write_text(
        "#!/bin/sh\nprintf '%s\\n' '" + json.dumps({"normalized_plan": plan}) + "'\n",
        encoding="utf-8",
    )
    dispatcher.chmod(0o755)
    (contract / "profile-control.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (contract / "profile-control.py").chmod(0o755)
    store.prepare_capsule(
        lease.run_id,
        contract_root=contract,
        scenario_slug=selected.slug,
        binding={
            "catalog_slug": selected.slug,
            "primary_profile": "load.north_south",
            "logical_profile_id": "F07-E.fixed.v1",
            "companion_profiles": [],
        },
    )
    runner = ScenarioRunner(
        tmp_path,
        tmp_path / "logs",
        coordinator=coordinator,
        artifact_store=store,
    )
    await runner.start(selected.id, "cleanup")
    assert runner._task is not None
    await runner._task

    assert (coordinator.snapshot().dirty_run is None) is cleanup_succeeds
    assert runner.get_current().dirty is (not cleanup_succeeds)


async def test_refused_capsule_repair_leaves_a_readable_reason(tmp_path, monkeypatch) -> None:
    """#21 (2026-08-03 batch): a refused capsule repair surfaced as HTTP 200
    with no capsule-repair.json and no error anywhere — the reason lived only
    in the process-local log ring. The refusal must land as a file next to the
    run it refused to repair."""
    selected = manifest("evaluation")
    monkeypatch.setattr("app.runner.get_scenario", lambda _: None)
    monkeypatch.setattr("app.runner.get_manifest", lambda _: selected)

    coordinator = GlobalCoordinator(tmp_path / "coordinator.json")
    runner_time = datetime.now(timezone.utc)
    lease = coordinator.acquire(
        run_id="dirty-run",
        scenario_id=selected.id,
        now=runner_time,
        lease_sec=30,
    )
    coordinator.mark_dirty(
        run_id=lease.run_id,
        fencing_token=lease.fencing_token,
        reason="initial cleanup failed",
        now=runner_time,
    )
    store = RunArtifactStore(tmp_path / "runs")
    (store.root / lease.run_id).mkdir(parents=True)

    def refuse(*args, **kwargs):
        raise RuntimeError("capsule repair would change the cleanup target")

    monkeypatch.setattr(store, "repair_capsule_contracts", refuse)
    runner = ScenarioRunner(
        tmp_path,
        tmp_path / "logs",
        coordinator=coordinator,
        artifact_store=store,
    )
    await runner.start(selected.id, "cleanup", repair_capsule=True)
    assert runner._task is not None
    await runner._task

    assert coordinator.snapshot().dirty_run is not None
    record = json.loads(
        (store.root / lease.run_id / "manual-cleanup-error.json").read_text()
    )
    assert record["repair_capsule"] is True
    assert "capsule repair would change the cleanup target" in record["error"]


# U9: a DIRTY run whose cleanup failed is cleaned again by the runner itself,
# but only after the node's own undo deadline (testbed-services U8: arm + max
# injection duration + 600s, or the last cleanup that reached the node + 960s).

U9_T0 = datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc)
U9_PROFILE = "load.north_south"
U9_LEVELS = [
    {"level_index": 0, "level_id": "low", "parameters": {"target_rps": 60}},
    {"level_index": 1, "level_id": "high", "parameters": {"target_rps": 70}},
]


class MovingClock:
    def __init__(self, now: datetime) -> None:
        self.at = now

    def now(self) -> datetime:
        return self.at


def minutes(value: float) -> timedelta:
    return timedelta(minutes=value)


def journal_applies(run_dir, slug: str, plan: dict, clock: MovingClock, applies) -> list:
    """Drive the real applier through the run's applies; returns the level changes.

    Each apply is ``(level, called_at, returned_at, outcome)``: profile-control
    reports ``applied_at = called_at`` (it takes the time before its executor
    runs) and returns at ``returned_at``. ``outcome`` is "ok", "failed"
    (profile-control exits non-zero) or "died" (the process went away in it)."""
    import subprocess

    from app.adaptive_runtime import ApplyRequest
    from app.production_runtime import TrustedDispatcherApplier as RealApplier

    contracts = run_dir / "capsule" / "contracts"
    current: dict = {}

    def process_runner(argv, **kwargs):
        if argv[0] == str(contracts / "run-scenario.sh"):
            return subprocess.CompletedProcess(argv, 0, json.dumps({"normalized_plan": plan}), "")
        clock.at = current["returned_at"]
        if current["outcome"] == "failed":
            raise subprocess.CalledProcessError(1, argv, "", "unit did not become active")
        if current["outcome"] == "died":
            raise RuntimeError("process went away mid-apply")
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"applied_at": current["called_at"].isoformat()}), ""
        )

    applier = RealApplier(
        slug,
        primary_profile=U9_PROFILE,
        dispatcher=contracts / "run-scenario.sh",
        profile_control=contracts / "profile-control.py",
        process_runner=process_runner,
        clock=clock,
        run_dir=run_dir,
    )
    changes = []
    for level, called_at, returned_at, outcome in applies:
        current.update(called_at=called_at, returned_at=returned_at, outcome=outcome)
        clock.at = called_at
        request = ApplyRequest(
            run_id="dirty-run",
            scenario_id="F07-E",
            fencing_token=1,
            profile_id=U9_PROFILE,
            level_index=level,
            level_id=U9_LEVELS[level]["level_id"],
            parameters=U9_LEVELS[level]["parameters"],
            idempotency_key=f"apply:dirty-run:1:{level}",
            requested_at=called_at,
        )
        if outcome == "ok":
            result = applier.apply(request)
            changes.append(
                {"level_index": level, "level_id": U9_LEVELS[level]["level_id"],
                 "applied_at": result.applied_at.isoformat()}
            )
        else:
            with pytest.raises(Exception):
                applier.apply(request)
    return changes


def dirty_session(
    level_changes: list, *, terminal: datetime, reason: str, cleanup_succeeded: bool = False
) -> dict:
    """state.json as the controller leaves it, reason through the real finalize_cleanup."""
    from app.adaptive import ControllerAction, ControllerPhase, ControllerState, finalize_cleanup

    terminal_state = ControllerState(
        phase=ControllerPhase.ABORTED, action=ControllerAction.ABORT, reason=reason
    )
    final = (
        finalize_cleanup(terminal_state, cleanup_succeeded=False)
        if not cleanup_succeeded
        else terminal_state
    )
    return {
        "status": "dirty",
        "scenario_id": "F07-E",
        "level_changes": level_changes,
        "controller_state": final.model_dump(mode="json"),
        "terminal_at": terminal.isoformat(),
        "cleanup": {
            "requested_at": terminal.isoformat(),
            "idempotency_key": "cleanup:dirty-run:1",
            "succeeded": cleanup_succeeded,
            "reason": None
            if cleanup_succeeded
            else "cleanup_exception:CalledProcessError: ssh: connect to host tb-w2 port 22: No route to host",
        },
    }


def dirty_cleanup_run(
    tmp_path,
    monkeypatch,
    *,
    marked_at: datetime,
    applies=((0, U9_T0, U9_T0, "ok"),),
    terminal: datetime | None = None,
    reason: str = "profile_apply_failed:CalledProcessError",
    write_session: bool = True,
    cleanup_succeeded: bool = False,
    flat_cleanup: dict | None = None,
    max_injection="20m",
    outcomes=None,
):
    """A DIRTY capsule run, as a failed node cleanup leaves it, and a runner on a movable clock.

    ``outcomes`` lists what successive retry cleanups report (True, or a failure reason)."""
    selected = manifest("evaluation")
    monkeypatch.setattr("app.runner.get_scenario", lambda _: None)
    monkeypatch.setattr("app.runner.get_manifest", lambda _: selected)
    calls: list = []
    pending = list(outcomes if outcomes is not None else [True])

    class Applier:
        def __init__(self, *args, **kwargs):
            pass

        def cleanup(self, request):
            calls.append((clock.now(), request))
            outcome = pending.pop(0) if pending else True
            if outcome is True:
                return SimpleNamespace(succeeded=True, reason=None, effect_ended_at=None)
            return SimpleNamespace(succeeded=False, reason=outcome, effect_ended_at=None)

    monkeypatch.setattr("app.runner.TrustedDispatcherApplier", Applier)
    clock = MovingClock(U9_T0)
    coordinator = GlobalCoordinator(tmp_path / "coordinator.json")
    lease = coordinator.acquire(
        run_id="dirty-run", scenario_id=selected.id, now=U9_T0, lease_sec=30
    )
    store = RunArtifactStore(tmp_path / "runs")
    contract = tmp_path / "contracts"
    contract.mkdir()
    plan = {
        "scenario": {"id": selected.id, "slug": selected.slug},
        "live_allowed": True,
        "plan_digest": "a" * 64,
        "profile_instances": [{"profile_id": U9_PROFILE, "approved_levels": U9_LEVELS}],
        "controller": {"runtime": {"mode": "evaluation", "max_injection_duration": max_injection}},
    }
    dispatcher = contract / "run-scenario.sh"
    dispatcher.write_text(
        "#!/bin/sh\nprintf '%s\\n' '" + json.dumps({"normalized_plan": plan}) + "'\n",
        encoding="utf-8",
    )
    dispatcher.chmod(0o755)
    (contract / "profile-control.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (contract / "profile-control.py").chmod(0o755)
    run_dir, _ = store.prepare_capsule(
        lease.run_id,
        contract_root=contract,
        scenario_slug=selected.slug,
        binding={
            "catalog_slug": selected.slug,
            "primary_profile": U9_PROFILE,
            "logical_profile_id": "F07-E.fixed.v1",
            "companion_profiles": [],
        },
    )
    changes = journal_applies(run_dir, selected.slug, plan, clock, applies)
    if write_session:
        store.persist_session(
            run_dir,
            dirty_session(
                changes,
                terminal=terminal or marked_at - minutes(1),
                reason=reason,
                cleanup_succeeded=cleanup_succeeded,
            ),
        )
    if flat_cleanup is not None:
        (run_dir / "cleanup.json").write_text(json.dumps(flat_cleanup), encoding="utf-8")
    coordinator.mark_dirty(
        run_id=lease.run_id,
        fencing_token=lease.fencing_token,
        reason="adaptive_controller_dirty",
        now=marked_at,
    )
    clock.at = marked_at
    runner = ScenarioRunner(
        tmp_path,
        tmp_path / "logs",
        coordinator=coordinator,
        artifact_store=store,
        clock=clock,
    )
    return SimpleNamespace(
        runner=runner, coordinator=coordinator, clock=clock, calls=calls, run_dir=run_dir
    )


async def settle(runner) -> str:
    """One retry check; if it started a cleanup, wait for it."""
    decision = await runner.retry_dirty_cleanup_when_due()
    if decision == "cleanup_retry_started":
        assert runner._task is not None
        await runner._task
    return decision


U9_DEADLINE_CASES = {
    # The node deadline counts from when the arming apply returned: + 20m + 10m, + 5m slack.
    "arm-bound": (dict(marked_at=U9_T0 + minutes(2)), minutes(35)),
    # A late failed cleanup postponed the node timer: marked + 16m + 5m.
    "cleanup-postponed": (dict(marked_at=U9_T0 + minutes(25)), minutes(25 + 16 + 5)),
    # An escalation applied later re-armed the node from there.
    "re-armed-by-escalation": (
        dict(
            marked_at=U9_T0 + minutes(10),
            applies=((0, U9_T0, U9_T0, "ok"), (1, U9_T0 + minutes(8), U9_T0 + minutes(8), "ok")),
            reason="success_conditions_met",
        ),
        minutes(8 + 30 + 5),
    ),
    # Review H1: the escalation's apply armed the node, then failed. The level
    # change list has only level 0, and the controller reason reads
    # "cleanup_failed after profile_apply_failed:...". The failed call's end bounds the arm.
    "escalation-failed-after-arm": (
        dict(
            marked_at=U9_T0 + minutes(10),
            applies=(
                (0, U9_T0, U9_T0, "ok"),
                (1, U9_T0 + minutes(8), U9_T0 + minutes(9), "failed"),
            ),
            terminal=U9_T0 + minutes(9),
        ),
        minutes(9 + 30 + 5),
    ),
    # Review M1: profile-control reports applied_at before its executor runs;
    # checks and the guard lock wait put the arm minutes later (here 4m).
    "slow-executor": (
        dict(marked_at=U9_T0 + minutes(5), applies=((0, U9_T0, U9_T0 + minutes(4), "ok"),)),
        minutes(4 + 30 + 5),
    ),
    # The process died inside an apply and the watchdog cleaned up: the session
    # never closed and the apply has only its intent. The DIRTY mark bounds every arm.
    "died-mid-apply": (
        dict(
            marked_at=U9_T0 + minutes(4),
            applies=((0, U9_T0, U9_T0, "died"),),
            write_session=False,
            flat_cleanup={
                "schema_version": 1,
                "source": "watchdog",
                "succeeded": False,
                "reason": "watchdog_cleanup_failed:CalledProcessError",
            },
        ),
        minutes(4 + 30 + 5),
    ),
}


@pytest.mark.parametrize("case", list(U9_DEADLINE_CASES))
async def test_dirty_cleanup_retry_waits_for_the_node_undo_deadline_then_clears_dirty(
    tmp_path, monkeypatch, case
) -> None:
    setup, first_retry_at = U9_DEADLINE_CASES[case]
    run = dirty_cleanup_run(tmp_path, monkeypatch, **setup)
    marked = setup["marked_at"]

    # Before the node's deadline: never call cleanup (it would race the node's own undo).
    for at in (marked, U9_T0 + first_retry_at - timedelta(seconds=1)):
        run.clock.at = at
        assert await settle(run.runner) == "waiting_for_node_undo_deadline"
    assert run.calls == []
    assert run.coordinator.snapshot().dirty_run is not None
    record = json.loads((run.run_dir / "dirty-retry.json").read_text())
    assert record["status"] == "waiting"
    assert record["not_before"] == (U9_T0 + first_retry_at).isoformat()

    run.clock.at = U9_T0 + first_retry_at
    assert await settle(run.runner) == "cleanup_retry_started"

    assert len(run.calls) == 1
    assert run.coordinator.snapshot().dirty_run is None
    assert run.coordinator.snapshot().cleanup_claim is None
    cleanup = json.loads((run.run_dir / "cleanup.json").read_text())
    assert cleanup["source"] == "dirty-cleanup-retry"
    assert cleanup["succeeded"] is True and cleanup["effect_ended_at"]
    record = json.loads((run.run_dir / "dirty-retry.json").read_text())
    assert record["status"] == "succeeded"
    assert [attempt["succeeded"] for attempt in record["attempts"]] == [True]
    assert run.runner.get_current().status == "succeeded"
    assert await settle(run.runner) == "no_dirty_run"
    # The run's effect interval now ends at the retry, so the queue's resume
    # cooldown (U2) counts from there, as after a manual cleanup.
    from app.live_probes import _run_intervals

    assert all(end <= U9_T0 + first_retry_at for _, end in _run_intervals(run.run_dir))


def test_the_applier_journals_when_a_failed_apply_call_ended(tmp_path, monkeypatch) -> None:
    """The retry's arm bound: a failed profile-control call leaves its end in
    mutations.json; a call the process never came back from leaves only its intent."""
    run = dirty_cleanup_run(
        tmp_path,
        monkeypatch,
        marked_at=U9_T0 + minutes(20),
        applies=(
            (0, U9_T0, U9_T0 + minutes(1), "ok"),
            (1, U9_T0 + minutes(8), U9_T0 + minutes(9), "failed"),
        ),
    )
    operations = json.loads((run.run_dir / "mutations.json").read_text())["operations"]
    assert [(op["complete_at"], op.get("failed_at")) for op in operations] == [
        ((U9_T0 + minutes(1)).isoformat(), None),
        (None, (U9_T0 + minutes(9)).isoformat()),
    ]


async def test_failed_dirty_cleanup_retries_stop_at_the_limit_and_keep_dirty(
    tmp_path, monkeypatch
) -> None:
    run = dirty_cleanup_run(
        tmp_path,
        monkeypatch,
        marked_at=U9_T0 + minutes(2),
        outcomes=["ssh refused 1", "ssh refused 2", "ssh refused 3", True],
    )
    assert run.runner.dirty_retry_policy.max_attempts == 3

    run.clock.at = U9_T0 + minutes(35)
    assert await settle(run.runner) == "cleanup_retry_started"
    assert run.coordinator.snapshot().dirty_run is not None

    # The failed attempt may have reached the node and postponed its timer by
    # 16m from when it ended: the next one waits that out plus the slack.
    after_first = run.clock.at
    run.clock.at = after_first + minutes(16 + 5) - timedelta(seconds=1)
    assert await settle(run.runner) == "waiting_for_node_undo_deadline"
    run.clock.at = after_first + minutes(16 + 5)
    assert await settle(run.runner) == "cleanup_retry_started"
    run.clock.at += minutes(21)
    assert await settle(run.runner) == "cleanup_retry_started"

    assert len(run.calls) == 3
    assert len({request.idempotency_key for _, request in run.calls}) == 3
    dirty = run.coordinator.snapshot().dirty_run
    assert dirty is not None and dirty.run_id == "dirty-run"
    assert run.coordinator.snapshot().cleanup_claim is None
    record = json.loads((run.run_dir / "dirty-retry.json").read_text())
    assert record["status"] == "gave_up"
    assert "stays DIRTY" in record["reason"] and "ssh refused 3" in record["reason"]
    assert [attempt["reason"] for attempt in record["attempts"]] == [
        "ssh refused 1",
        "ssh refused 2",
        "ssh refused 3",
    ]
    assert any("stays DIRTY" in line for line in run.runner.get_current().log_tail)

    run.clock.at += timedelta(days=1)
    assert await settle(run.runner) == "dirty_retry_gave_up"
    assert len(run.calls) == 3


async def test_the_retry_holds_the_runner_until_its_outcome_is_recorded(
    tmp_path, monkeypatch
) -> None:
    """Review L1: the cleanup released the runner before the attempt's outcome
    was written, and a waiting note written in that gap erased the outcome and
    the give-up reason. The runner stays held until the record is final, and
    a waiting note never overwrites a final record."""
    import asyncio

    from app import dirty_retry

    run = dirty_cleanup_run(
        tmp_path,
        monkeypatch,
        marked_at=U9_T0 + minutes(2),
        outcomes=["ssh refused 1", "ssh refused 2", "ssh refused 3"],
    )
    run.runner.dirty_retry_policy = dirty_retry.RetryPolicy(max_attempts=1)
    finished = dirty_retry.note_finished

    def slow_note_finished(*args, **kwargs):
        # Runs on a worker thread after the cleanup has returned and released
        # its claim: the window the review found.
        import time

        time.sleep(0.2)
        return finished(*args, **kwargs)

    monkeypatch.setattr(dirty_retry, "note_finished", slow_note_finished)
    run.clock.at = U9_T0 + minutes(35)
    assert await run.runner.retry_dirty_cleanup_when_due() == "cleanup_retry_started"
    for _ in range(500):
        if run.runner.get_current().status != "cleanup_running":
            break
        await asyncio.sleep(0.01)
    assert run.runner.get_current().status == "failed"
    assert run.coordinator.snapshot().cleanup_claim is None
    # The cleanup is over but its outcome is not written yet: no new decision.
    assert await run.runner.retry_dirty_cleanup_when_due() == "runner_busy"
    await run.runner._task

    record = json.loads((run.run_dir / "dirty-retry.json").read_text())
    assert record["status"] == "gave_up" and "ssh refused 1" in record["reason"]
    waiting = dirty_retry.RetryDecision(
        due=False, reason="waiting_for_node_undo_deadline", not_before=run.clock.at
    )
    dirty = run.coordinator.snapshot().dirty_run
    dirty_retry.note_waiting(run.run_dir, dirty, waiting)
    assert json.loads((run.run_dir / "dirty-retry.json").read_text()) == record


async def test_a_retry_attempt_that_raises_counts_and_keeps_dirty(tmp_path, monkeypatch) -> None:
    run = dirty_cleanup_run(tmp_path, monkeypatch, marked_at=U9_T0 + minutes(2))

    def broken(_run_dir):
        raise RuntimeError("capsule contract hash mismatch")

    monkeypatch.setattr(run.runner.artifact_store, "verify_capsule", broken)
    run.clock.at = U9_T0 + minutes(35)
    assert await settle(run.runner) == "cleanup_retry_started"

    assert run.calls == []
    assert run.coordinator.snapshot().dirty_run is not None
    assert run.coordinator.snapshot().cleanup_claim is None
    record = json.loads((run.run_dir / "dirty-retry.json").read_text())
    assert record["status"] == "waiting"
    assert "capsule contract hash mismatch" in record["attempts"][0]["reason"]
    # The raised attempt's end pushes the next one out like any failed cleanup.
    run.clock.at += minutes(21) - timedelta(seconds=1)
    assert await settle(run.runner) == "waiting_for_node_undo_deadline"


async def test_an_attempt_cut_off_with_its_process_counts_from_this_process_start(
    tmp_path, monkeypatch
) -> None:
    """Review M2: an attempt the previous process never finished may have
    reached a node (and postponed its timer) any time until that process was
    gone. Its start alone is too early; this process's start bounds it."""
    from app import dirty_retry

    run = dirty_cleanup_run(tmp_path, monkeypatch, marked_at=U9_T0 + minutes(2))
    dirty = run.coordinator.snapshot().dirty_run
    dirty_retry.note_started(
        run.run_dir, dirty, claimant="cleanup-retry-gone", started_at=U9_T0 + minutes(35)
    )
    # The new process starts an hour later.
    run.clock.at = U9_T0 + minutes(95)
    restarted = ScenarioRunner(
        tmp_path,
        tmp_path / "logs",
        coordinator=run.coordinator,
        artifact_store=run.runner.artifact_store,
        clock=run.clock,
    )
    assert await settle(restarted) == "waiting_for_node_undo_deadline"
    record = json.loads((run.run_dir / "dirty-retry.json").read_text())
    assert record["not_before"] == (U9_T0 + minutes(95 + 16 + 5)).isoformat()
    run.clock.at = U9_T0 + minutes(95 + 16 + 5)
    assert await settle(restarted) == "cleanup_retry_started"
    assert run.coordinator.snapshot().dirty_run is None


async def test_a_claim_left_by_a_killed_attempt_is_reported_not_waited_on_silently(
    tmp_path, monkeypatch
) -> None:
    """Review M2: a process killed mid-attempt leaves its cleanup claim, which
    only its holder releases. The retry cannot run; it says so once, in the
    record and the log, and goes on once the claim is gone."""
    run = dirty_cleanup_run(tmp_path, monkeypatch, marked_at=U9_T0 + minutes(2))
    token = run.coordinator.snapshot().dirty_run.fencing_token
    run.coordinator.claim_cleanup(
        run_id="dirty-run", fencing_token=token, claimant="cleanup-retry-killed",
        now=U9_T0 + minutes(35),
    )
    run.clock.at = U9_T0 + minutes(60)
    assert await settle(run.runner) == "cleanup_already_claimed"
    assert await settle(run.runner) == "cleanup_already_claimed"
    record = json.loads((run.run_dir / "dirty-retry.json").read_text())
    assert record["status"] == "blocked"
    assert "cleanup-retry-killed" in record["reason"]
    assert sum("blocked" in line for line in run.runner._log_buffer) == 1
    assert run.calls == []

    run.coordinator.finish_cleanup_claim(
        run_id="dirty-run", fencing_token=token, claimant="cleanup-retry-killed"
    )
    assert await settle(run.runner) == "cleanup_retry_started"
    assert run.coordinator.snapshot().dirty_run is None


@pytest.mark.parametrize("reason", ["recovery_timeout", "heartbeat_failed"])
async def test_dirty_cleanup_retry_leaves_a_run_whose_cleanup_succeeded(
    tmp_path, monkeypatch, reason
) -> None:
    """DIRTY after a successful cleanup (recovery never came, the heartbeat
    broke) is not something a second cleanup can fix; clearing it that way
    would hide it."""
    run = dirty_cleanup_run(
        tmp_path,
        monkeypatch,
        marked_at=U9_T0 + minutes(12),
        reason=reason,
        cleanup_succeeded=True,
    )
    run.clock.at = U9_T0 + timedelta(hours=6)
    assert await settle(run.runner) == "dirty_not_from_a_failed_cleanup"
    assert run.calls == []
    assert run.coordinator.snapshot().dirty_run is not None


async def test_dirty_cleanup_retry_yields_to_whatever_holds_the_testbed(
    tmp_path, monkeypatch
) -> None:
    run = dirty_cleanup_run(tmp_path, monkeypatch, marked_at=U9_T0 + minutes(2))
    run.clock.at = U9_T0 + minutes(40)

    run.runner.cleanup_retry_hold = lambda: True
    assert await settle(run.runner) == "live_queue_restoring_or_injecting"
    run.runner.cleanup_retry_hold = lambda: False

    run.runner._starting = True
    assert await settle(run.runner) == "runner_busy"
    run.runner._starting = False
    assert run.calls == []

    # A failed manual cleanup in the meantime may have postponed the node timer too.
    (run.run_dir / "manual-cleanup-error.json").write_text(
        json.dumps({"at": (U9_T0 + minutes(30)).isoformat()}), encoding="utf-8"
    )
    assert await settle(run.runner) == "waiting_for_node_undo_deadline"
    run.clock.at = U9_T0 + minutes(30 + 16 + 5)
    assert await settle(run.runner) == "cleanup_retry_started"
    assert run.coordinator.snapshot().dirty_run is None


async def test_watchdog_loop_runs_the_dirty_cleanup_retry(tmp_path, monkeypatch) -> None:
    run = dirty_cleanup_run(tmp_path, monkeypatch, marked_at=U9_T0 + minutes(2))
    run.clock.at = U9_T0 + minutes(35)

    class Stop(Exception):
        pass

    async def one_round(_seconds):
        raise Stop()

    run.runner.sleeper = one_round
    with pytest.raises(Stop):
        await run.runner._watchdog_loop()
    assert run.runner._task is not None
    await run.runner._task
    assert run.coordinator.snapshot().dirty_run is None


def test_dirty_retry_settings_never_call_before_the_node_and_never_stop_the_runner(
    monkeypatch,
) -> None:
    """Review L2: a zero margin, hold or slack called cleanup before the node's
    own deadline, and a malformed value stopped the runner from starting."""
    from app import dirty_retry

    for name in (
        "DIRTY_RETRY_NODE_UNDO_MARGIN_SEC",
        "DIRTY_RETRY_NODE_UNDO_HOLD_SEC",
        "DIRTY_RETRY_SLACK_SEC",
    ):
        monkeypatch.setenv(name, "0")
    monkeypatch.setenv("DIRTY_RETRY_MAX_ATTEMPTS", "many")
    policy = dirty_retry.RetryPolicy.from_env()
    assert policy == dirty_retry.RetryPolicy(
        max_attempts=3, slack_sec=60, node_margin_sec=600, node_hold_sec=960
    )
    monkeypatch.setenv("DIRTY_RETRY_SLACK_SEC", "five minutes")
    monkeypatch.setenv("DIRTY_RETRY_NODE_UNDO_HOLD_SEC", "20m")
    policy = dirty_retry.RetryPolicy.from_env()
    assert (policy.slack_sec, policy.node_hold_sec) == (300, 1200)
    with pytest.raises(ValueError):
        dirty_retry.RetryPolicy(node_margin_sec=0)


def test_dirty_retry_node_constants_match_testbed_services() -> None:
    """The node's own undo margin, cleanup hold and default injection limit,
    copied here, must be the values the node guard uses."""
    import re
    from pathlib import Path

    from app import dirty_retry

    source = Path(__file__).resolve().parents[2].parent / (
        "testbed-services/scripts/scenarios/profiles/executor_common.py"
    )
    if not source.is_file():
        pytest.skip("external testbed-services checkout is not present")
    text = source.read_text(encoding="utf-8")

    def constant(name: str) -> int:
        match = re.search(rf"^{name} = (\d+)$", text, re.MULTILINE)
        assert match, name
        return int(match.group(1))

    assert constant("GUARD_MARGIN_SECONDS") == dirty_retry.NODE_UNDO_MARGIN_SEC
    assert constant("GUARD_CLEANUP_HOLD_SECONDS") == dirty_retry.NODE_UNDO_HOLD_SEC
    assert constant("DEFAULT_MAX_INJECTION_SECONDS") == dirty_retry.DEFAULT_MAX_INJECTION_SEC


def test_every_live_controller_observation_binds_against_the_runner_registry():
    """Cross-repo contract: testbed-services controllers may only reference
    observation queries this runner can actually serve. This is the permanent
    form of the 2026-07-21 audit that caught 7 latent per-scenario mines
    (F09-R/F05-R/F05-H/F05-P) which would each have burned a live run."""
    import json
    from pathlib import Path

    from app.observations import ApprovedQueryRegistry

    controllers_path = Path(__file__).resolve().parents[2].parent / (
        "testbed-services/scripts/scenarios/registry/controllers.json"
    )
    if not controllers_path.is_file():
        import pytest

        pytest.skip("external controllers registry is not checked out")
    controllers = json.loads(controllers_path.read_text())
    registry = ApprovedQueryRegistry.from_path()
    problems = []
    for scenario_id, controller in controllers["controllers"].items():
        for observation in controller["observations"]:
            spec = {"query_id": observation["query_id"]}
            if observation.get("parameters"):
                spec["parameters"] = observation["parameters"]
            try:
                registry.bind(spec)
            except Exception as error:
                problems.append(f"{scenario_id}/{observation['id']}: {error}")
    assert not problems, "\n".join(problems)


def test_every_live_controller_observation_passes_the_probe_allowlists():
    """Binding at the query registry is not enough: the live probes re-check the
    resolved parameters against their own allowlists (APPROVED_APM_SERVICES,
    THROTTLE_TARGETS, APPROVED_NODE_TARGETS, ...) just before issuing the query.

    F21-P's 2026-07-31 calibration run is why this exists. Its throttle
    observation bound cleanly against the registry and was rejected at probe
    time because the banking target was missing from THROTTLE_TARGETS, so the
    scenario spent an entire live run unable to observe its own injected cause.
    The sibling test above would not have caught it.
    """
    from pathlib import Path

    from app.live_probes import LiveProbeError, LiveProbeSet
    from app.observations import ApprovedQueryRegistry

    controllers_path = Path(__file__).resolve().parents[2].parent / (
        "testbed-services/scripts/scenarios/registry/controllers.json"
    )
    catalog_path = Path(__file__).resolve().parents[2].parent / (
        "testbed-services/scripts/scenarios/catalog.json"
    )
    if not controllers_path.is_file() or not catalog_path.is_file():
        pytest.skip("external scenario registry is not checked out")

    ready = {
        row["id"]
        for row in json.loads(catalog_path.read_text())["scenarios"]
        if row["readiness"] == "ready"
    }
    controllers = json.loads(controllers_path.read_text())["controllers"]
    registry = ApprovedQueryRegistry.from_path()

    class _Reached(Exception):
        """Raised by the stub transport once parameter validation has passed."""

    def _stub(*args, **kwargs):
        raise _Reached()

    probes = LiveProbeSet(
        process_runner=_stub,
        http_client=_stub,
        database_client=_stub,
        database_credentials={},
    )
    # Only the adapters that gate on a parameter allowlist; the others need a
    # live host or a subprocess and are covered by their own tests.
    # `database` belongs here even though it shells out: its allowlist check runs
    # before the subprocess, and everything past validation is swallowed below.
    # Leaving it out is how the Oracle session tag drifted out of sync with the
    # manifests unnoticed until it wedged the live queue (2026-08-03).
    # `kubernetes` is the same shape: APPROVED_K8S_TARGETS is checked before
    # kubectl runs. It was the one allowlist surface with no guard, which is how
    # F14-P's recovery selector (banking/testbed-ledger) stayed unapproved
    # through the whole 2026-08-03 batch and aged every run into DIRTY.
    guarded = {
        "prometheus": probes._prometheus_observation,
        "clickhouse": probes._clickhouse_observation,
        "database": probes._database_observation,
        "loadgen_summary": probes._loadgen_observation,
        "kubernetes": probes._kubernetes_observation,
    }

    problems = []
    for scenario_id in sorted(ready & set(controllers)):
        for observation in controllers[scenario_id]["observations"]:
            probe = guarded.get(observation.get("adapter"))
            if probe is None:
                continue
            if observation["adapter"] == "loadgen_summary" and not observation.get("parameters"):
                # The unparameterized loadgen path reads the *running* scenario's own
                # k6 output, so it needs a live run context this test cannot supply.
                # The parameterized (domain) form is the one with an allowlist to check.
                continue
            spec = {"query_id": observation["query_id"]}
            if observation.get("parameters"):
                spec["parameters"] = observation["parameters"]
            try:
                probe(registry.bind(spec))
            except _Reached:
                continue
            except LiveProbeError as error:
                problems.append(f"{scenario_id}/{observation['id']}: {error}")
            except Exception:
                # Anything past validation (transport, parsing) is out of scope.
                continue
    assert not problems, "\n".join(problems)
