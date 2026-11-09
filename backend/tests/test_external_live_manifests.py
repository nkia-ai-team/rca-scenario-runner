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


class MovingClock:
    def __init__(self, now: datetime) -> None:
        self.at = now

    def now(self) -> datetime:
        return self.at


def dirty_cleanup_run(
    tmp_path,
    monkeypatch,
    *,
    marked_at: datetime,
    session: dict | None,
    flat_cleanup: dict | None = None,
    max_injection="20m",
    outcomes=None,
):
    """A DIRTY capsule run, as a failed node cleanup leaves it, and a runner on a movable clock.

    ``outcomes`` lists what successive cleanup calls report (True, or a failure reason)."""
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
    clock = MovingClock(marked_at)
    coordinator = GlobalCoordinator(tmp_path / "coordinator.json")
    lease = coordinator.acquire(
        run_id="dirty-run", scenario_id=selected.id, now=U9_T0, lease_sec=30
    )
    coordinator.mark_dirty(
        run_id=lease.run_id,
        fencing_token=lease.fencing_token,
        reason="adaptive_controller_dirty",
        now=marked_at,
    )
    store = RunArtifactStore(tmp_path / "runs")
    contract = tmp_path / "contracts"
    contract.mkdir()
    plan = {
        "scenario": {"id": selected.id, "slug": selected.slug},
        "live_allowed": True,
        "plan_digest": "a" * 64,
        "profile_instances": [],
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
            "primary_profile": "load.north_south",
            "logical_profile_id": "F07-E.fixed.v1",
            "companion_profiles": [],
        },
    )
    if session is not None:
        store.persist_session(run_dir, session)
    if flat_cleanup is not None:
        (run_dir / "cleanup.json").write_text(json.dumps(flat_cleanup), encoding="utf-8")
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


def failed_cleanup_session(
    *, applied: list[datetime], terminal: datetime, reason: str = "success_conditions_met"
) -> dict:
    return {
        "status": "dirty",
        "scenario_id": "F07-E",
        "level_changes": [
            {"level_index": 0, "level_id": "fixed", "applied_at": moment.isoformat()}
            for moment in applied
        ],
        "controller_state": {"phase": "succeeded", "reason": reason},
        "terminal_at": terminal.isoformat(),
        "cleanup": {
            "requested_at": terminal.isoformat(),
            "idempotency_key": "cleanup:dirty-run:1",
            "succeeded": False,
            "reason": "cleanup_exception:CalledProcessError: ssh: connect to host tb-w2 port 22: No route to host",
        },
    }


async def settle(runner) -> str:
    """One retry check; if it started a cleanup, wait for it."""
    decision = await runner.retry_dirty_cleanup_when_due()
    if decision == "cleanup_retry_started":
        assert runner._task is not None
        await runner._task
    return decision


def minutes(value: float) -> timedelta:
    return timedelta(minutes=value)


@pytest.mark.parametrize(
    ("case", "first_retry_at"),
    [
        # Node deadline from the arm: last apply + 20m + 10m, plus 5m slack.
        ("arm-bound", minutes(35)),
        # A late failed cleanup postponed the node timer: marked + 16m + 5m.
        ("cleanup-postponed", minutes(25 + 16 + 5)),
        # An escalation applied later re-armed the node from there.
        ("re-armed-by-escalation", minutes(8 + 30 + 5)),
        # An apply that failed may have armed before failing: terminal_at bounds it.
        ("apply-failed-after-arm", minutes(12 + 30 + 5)),
        # The session never closed (watchdog cleanup): the DIRTY mark bounds every arm.
        ("watchdog-no-session", minutes(4 + 30 + 5)),
    ],
)
async def test_dirty_cleanup_retry_waits_for_the_node_undo_deadline_then_clears_dirty(
    tmp_path, monkeypatch, case, first_retry_at
) -> None:
    session = failed_cleanup_session(applied=[U9_T0], terminal=U9_T0 + minutes(1))
    marked = U9_T0 + minutes(2)
    flat_cleanup = None
    if case == "cleanup-postponed":
        marked = U9_T0 + minutes(25)
    elif case == "re-armed-by-escalation":
        session = failed_cleanup_session(
            applied=[U9_T0, U9_T0 + minutes(8)], terminal=U9_T0 + minutes(9)
        )
        marked = U9_T0 + minutes(10)
    elif case == "apply-failed-after-arm":
        session = failed_cleanup_session(
            applied=[U9_T0],
            terminal=U9_T0 + minutes(12),
            reason="profile_apply_failed:CalledProcessError",
        )
        marked = U9_T0 + minutes(13)
    elif case == "watchdog-no-session":
        session = None
        marked = U9_T0 + minutes(4)
        flat_cleanup = {
            "schema_version": 1,
            "source": "watchdog",
            "succeeded": False,
            "reason": "watchdog_cleanup_failed:CalledProcessError",
        }
    run = dirty_cleanup_run(
        tmp_path, monkeypatch, marked_at=marked, session=session, flat_cleanup=flat_cleanup
    )

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


async def test_failed_dirty_cleanup_retries_stop_at_the_limit_and_keep_dirty(
    tmp_path, monkeypatch
) -> None:
    run = dirty_cleanup_run(
        tmp_path,
        monkeypatch,
        marked_at=U9_T0 + minutes(2),
        session=failed_cleanup_session(applied=[U9_T0], terminal=U9_T0 + minutes(1)),
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


async def test_a_retry_attempt_that_raises_counts_and_keeps_dirty(tmp_path, monkeypatch) -> None:
    run = dirty_cleanup_run(
        tmp_path,
        monkeypatch,
        marked_at=U9_T0 + minutes(2),
        session=failed_cleanup_session(applied=[U9_T0], terminal=U9_T0 + minutes(1)),
    )

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


@pytest.mark.parametrize(
    "reason",
    ["recovery_timeout", "heartbeat_failed"],
)
async def test_dirty_cleanup_retry_leaves_a_run_whose_cleanup_succeeded(
    tmp_path, monkeypatch, reason
) -> None:
    """DIRTY after a successful cleanup (recovery never came, the heartbeat
    broke) is not something a second cleanup can fix; clearing it that way
    would hide it."""
    session = failed_cleanup_session(applied=[U9_T0], terminal=U9_T0 + minutes(1))
    session["cleanup"]["succeeded"] = True
    session["controller_state"]["reason"] = reason
    run = dirty_cleanup_run(
        tmp_path, monkeypatch, marked_at=U9_T0 + minutes(12), session=session
    )
    run.clock.at = U9_T0 + timedelta(hours=6)
    assert await settle(run.runner) == "dirty_not_from_a_failed_cleanup"
    assert run.calls == []
    assert run.coordinator.snapshot().dirty_run is not None


async def test_dirty_cleanup_retry_yields_to_whatever_holds_the_testbed(
    tmp_path, monkeypatch
) -> None:
    run = dirty_cleanup_run(
        tmp_path,
        monkeypatch,
        marked_at=U9_T0 + minutes(2),
        session=failed_cleanup_session(applied=[U9_T0], terminal=U9_T0 + minutes(1)),
    )
    run.clock.at = U9_T0 + minutes(40)

    run.runner.cleanup_retry_hold = lambda: True
    assert await settle(run.runner) == "live_queue_restoring_or_injecting"
    run.runner.cleanup_retry_hold = lambda: False

    run.runner._starting = True
    assert await settle(run.runner) == "runner_busy"
    run.runner._starting = False

    run.coordinator.claim_cleanup(
        run_id="dirty-run",
        fencing_token=run.coordinator.snapshot().dirty_run.fencing_token,
        claimant="manual-cleanup-elsewhere",
        now=run.clock.at,
    )
    assert await settle(run.runner) == "cleanup_already_claimed"
    assert run.calls == []
    run.coordinator.finish_cleanup_claim(
        run_id="dirty-run",
        fencing_token=run.coordinator.snapshot().dirty_run.fencing_token,
        claimant="manual-cleanup-elsewhere",
    )

    # A failed manual cleanup in the meantime may have postponed the node timer too.
    (run.run_dir / "manual-cleanup-error.json").write_text(
        json.dumps({"at": (U9_T0 + minutes(30)).isoformat()}), encoding="utf-8"
    )
    assert await settle(run.runner) == "waiting_for_node_undo_deadline"
    run.clock.at = U9_T0 + minutes(30 + 16 + 5)
    assert await settle(run.runner) == "cleanup_retry_started"
    assert run.coordinator.snapshot().dirty_run is None


async def test_watchdog_loop_runs_the_dirty_cleanup_retry(tmp_path, monkeypatch) -> None:
    run = dirty_cleanup_run(
        tmp_path,
        monkeypatch,
        marked_at=U9_T0 + minutes(2),
        session=failed_cleanup_session(applied=[U9_T0], terminal=U9_T0 + minutes(1)),
    )
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
