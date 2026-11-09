from __future__ import annotations

import json
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.controller import parse_controller
from app.coordinator import GlobalCoordinator
from app.manifests import get_manifest
from app.runner import ScenarioRunner, execution_environment
from app.watchdog import decide_watchdog


def _conditions(signal: str, *, match: str = "all") -> dict:
    return {match: [{"observation": signal, "op": "eq", "value": True}]}


def _controller_yaml(*, mode: str = "calibration") -> dict:
    levels = [
        {"id": "l1", "parameters": {"target_rps": 60}, "min_hold": "1m"},
        {"id": "l2", "parameters": {"target_rps": 70}, "min_hold": "2m"},
    ]
    if mode == "evaluation":
        levels = [levels[1]]
    raw = {
        "mode": mode,
        "tick_interval": "15s",
        "settle_after_change": "30s",
        "max_injection_duration": "10m",
        "preflight": ["canonical-kubeconfig", "global-dirty-lease", "target-health"],
        "baseline": {
            "clean_window": "30m",
            "required": [{"check": "baseline-clean"}, {"check": "target-health"}],
        },
        "profile": {
            "kind": "fixed" if mode == "evaluation" else "adaptive_ladder",
            "levels": levels,
        },
        "observations": [
            {
                "id": "target_ok",
                "adapter": "prometheus",
                "query_id": "approved.target.ok.v1",
                "freshness": "1m",
            },
            {
                "id": "alternative_found",
                "adapter": "database",
                "query_id": "approved.db.alternative.v1",
                "freshness": "30s",
            },
            {
                "id": "recovered",
                "adapter": "http_probe",
                "query_id": "approved.recovery.v1",
                "freshness": "30s",
            },
        ],
        "success": _conditions("target_ok"),
        "must_rule_out": _conditions("alternative_found", match="any"),
        "abort": _conditions("target_ok", match="any"),
        "recovery": {**_conditions("recovered"), "timeout": "5m"},
        "cleanup": {"required": True, "order": "reverse", "timeout": "3m"},
        "capture": {
            "pre_window": "10m",
            "post_window": "20m",
            "model_snapshot": "/var/lib/lucida/ai-models/stream-anomaly/global/v1/model.json",
            "create_golden_anomaly": False,
        },
    }
    if mode == "calibration":
        raw["escalate"] = {
            "all": [{"observation": "target_ok", "op": "eq", "value": False}]
        }
    return raw


def test_declarative_controller_compiles_to_normalized_adaptive_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = parse_controller(_controller_yaml())
    assert spec is not None
    assert spec.tick_interval_sec == 15
    assert spec.baseline.clean_window_sec == 1800
    assert spec.adaptive.levels[1].min_hold_sec == 120
    assert spec.adapter_queries[0].query_id == "approved.target.ok.v1"

    script = tmp_path / "scenario.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    from app.scenarios import SCENARIOS

    scenario = next(iter(SCENARIOS.values())).model_copy(
        update={"script_filename": script.name, "controller": spec}
    )
    monkeypatch.setattr("app.runner.get_scenario", lambda _: scenario)
    plan = ScenarioRunner(tmp_path, tmp_path / "logs").dry_run(scenario.id)
    assert plan["side_effects"] is False
    assert plan["controller"]["adaptive"]["mode"] == "calibration"
    assert plan["controller"]["preflight_checks"][0] == "canonical-kubeconfig"
    assert plan["controller"]["adapter_queries"][0]["query_id"].startswith("approved.")
    assert plan["controller"]["cleanup"]["order"] == "reverse"
    assert plan["controller"]["recovery"]["timeout_sec"] == 300
    assert plan["controller"]["capture"]["execution"] == "planned_only"
    assert plan["controller"]["capture"]["command"][0] == "capture-eval-case.sh"
    assert not (tmp_path / "logs").exists()


def test_scenario_loader_accepts_declarative_controller_yaml() -> None:
    from app.scenarios import _spec_entry_to_scenario

    scenario = _spec_entry_to_scenario(
        "demo",
        "Demo",
        {
            "id": "scenario-01",
            "file": "scenario.sh",
            "title": "demo",
            "description": "demo",
            "root_cause": "demo cause",
            "propagation": "demo propagation",
            "estimated_duration_sec": 60,
            "controller": _controller_yaml(mode="evaluation"),
        },
    )
    assert scenario.controller is not None
    assert scenario.controller.adaptive.mode == "evaluation"
    assert scenario.controller.adapter_queries[0].query_id == "approved.target.ok.v1"


def test_controller_rejects_raw_adapter_query_and_unbound_signal() -> None:
    raw = _controller_yaml()
    raw["observations"][0].pop("query_id")
    raw["observations"][0]["query"] = "up{job='unsafe'}"
    with pytest.raises(ValueError, match="approved query_id"):
        parse_controller(raw)

    raw = _controller_yaml()
    raw["success"] = _conditions("undeclared")
    with pytest.raises(ValueError, match="undeclared observations"):
        parse_controller(raw)


def test_evaluation_requires_one_fixed_level() -> None:
    raw = _controller_yaml(mode="evaluation")
    spec = parse_controller(raw)
    assert spec is not None and len(spec.adaptive.levels) == 1
    raw["profile"]["levels"].append(raw["profile"]["levels"][0])
    with pytest.raises(ValueError, match="exactly one level"):
        parse_controller(raw)


def _manifest() -> dict:
    return {
        "schema_version": "1.0",
        "id": "F01-G",
        "slug": "f01-g-test",
        "readiness": "partial",
        "injection": {"location": "mock", "transport": "api-via-kubectl"},
        "execution": {
            "preflight_ids": ["canonical-kubeconfig", "global-dirty-lease"],
            "controller": {
                "dispatcher_mode": "dry-run",
                "decision_mode": "evaluation",
                "live_enabled": False,
            },
        },
        "actions": {
            "plan": {"mode": "dry-run", "mutation": False, "adapter_refs": []},
            "run": {"mode": "dry-run", "mutation": False, "adapter_refs": ["mock"]},
            "cleanup": {
                "mode": "dry-run", "mutation": False, "required": True,
                "order": "reverse", "recovery_gate": True, "adapter_refs": ["mock"],
            },
        },
        "capture_policy": {"create_golden_anomaly": False, "time_basis": "UTC"},
    }


def test_external_manifest_root_is_imported_as_plan_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "manifests"
    root.mkdir()
    (root / "f01-g-test.yaml").write_text(json.dumps(_manifest()), encoding="utf-8")
    monkeypatch.setenv("SCENARIO_MANIFEST_ROOT", str(root))
    assert get_manifest("F01-G").slug == "f01-g-test"
    plan = ScenarioRunner(tmp_path / "scripts", tmp_path / "logs").dry_run("f01-g-test")
    assert plan["side_effects"] is False
    assert plan["preflight_checks"] == ["canonical-kubeconfig", "global-dirty-lease"]
    assert plan["cleanup"]["recovery_gate"] is True
    assert plan["capture"]["execution"] == "planned_only"
    assert plan["capture"]["command"] is None


def test_external_manifest_import_rejects_live_or_mutating_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "manifests"
    root.mkdir()
    manifest = _manifest()
    manifest["actions"]["run"]["mutation"] = True
    (root / "unsafe.yaml").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("SCENARIO_MANIFEST_ROOT", str(root))
    with pytest.raises(ValueError, match="side-effect-free"):
        get_manifest("f01-g-test")


def test_global_coordinator_persists_lease_dirty_block_and_fenced_cleanup(tmp_path: Path) -> None:
    now = datetime(2026, 7, 16, tzinfo=timezone.utc)
    path = tmp_path / "coordinator.json"
    coordinator = GlobalCoordinator(path)
    lease = coordinator.acquire(
        run_id="run-1", scenario_id="commerce:01", now=now, lease_sec=60
    )
    assert GlobalCoordinator(path).snapshot().active_lease == lease
    with pytest.raises(RuntimeError, match="active lease"):
        coordinator.acquire(run_id="run-2", scenario_id="food:01", now=now, lease_sec=60)
    with pytest.raises(RuntimeError, match="fencing token"):
        coordinator.heartbeat(run_id="run-1", fencing_token=lease.fencing_token + 1, now=now, lease_sec=60)

    dirty = coordinator.mark_dirty(
        run_id="run-1", fencing_token=lease.fencing_token, reason="cleanup_failed", now=now
    )
    with pytest.raises(RuntimeError, match="dirty run"):
        coordinator.acquire(run_id="run-2", scenario_id="food:01", now=now, lease_sec=60)
    decision = decide_watchdog(coordinator.snapshot(), now=now, heartbeat_timeout_sec=30)
    assert decision.action == "block_dirty"
    coordinator.claim_cleanup(
        run_id=dirty.run_id,
        fencing_token=dirty.fencing_token,
        claimant="watchdog-a",
        now=now,
    )
    with pytest.raises(RuntimeError, match="successful recovery"):
        coordinator.clear_dirty(
            run_id=dirty.run_id, fencing_token=dirty.fencing_token, recovery_verified=False
        )
    coordinator.clear_dirty(
        run_id=dirty.run_id, fencing_token=dirty.fencing_token, recovery_verified=True
    )
    next_lease = coordinator.acquire(
        run_id="run-2", scenario_id="food:01", now=now, lease_sec=60
    )
    assert next_lease.fencing_token > lease.fencing_token


def test_watchdog_requests_cleanup_only_after_timeout(tmp_path: Path) -> None:
    now = datetime(2026, 7, 16, tzinfo=timezone.utc)
    coordinator = GlobalCoordinator(tmp_path / "state.json")
    coordinator.acquire(run_id="run-1", scenario_id="commerce:01", now=now, lease_sec=30)
    healthy = decide_watchdog(
        coordinator.snapshot(), now=now + timedelta(seconds=10), heartbeat_timeout_sec=20
    )
    expired = decide_watchdog(
        coordinator.snapshot(), now=now + timedelta(seconds=31), heartbeat_timeout_sec=20
    )
    assert healthy.action == "healthy"
    assert expired.action == "claim_cleanup"
    assert expired.fencing_token == 1


def test_default_kubeconfig_is_canonical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KUBECONFIG", raising=False)
    assert execution_environment()["KUBECONFIG"] == "/root/tb-kubeconfig"
    monkeypatch.setenv("KUBECONFIG", "/explicit/config")
    assert execution_environment()["KUBECONFIG"] == "/root/tb-kubeconfig"


def _runner_scenario(script: Path, *, controller=None):
    from app.scenarios import SCENARIOS

    return next(iter(SCENARIOS.values())).model_copy(
        update={"script_filename": script.name, "controller": controller}
    )


async def test_runner_success_uses_fenced_lease_and_releases_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "scenario.sh"
    script.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    scenario = _runner_scenario(script)
    monkeypatch.setattr("app.runner.get_scenario", lambda _: scenario)
    coordinator = GlobalCoordinator(tmp_path / "state.json")
    runner = ScenarioRunner(tmp_path, tmp_path / "logs", coordinator=coordinator)

    started = await runner.start(scenario.id, "run")
    assert started.fencing_token == 1
    assert coordinator.snapshot().active_lease is not None
    assert runner._task is not None
    await runner._task

    finished = runner.get_current()
    assert finished is not None and finished.status == "succeeded"
    assert finished.dirty is False
    assert coordinator.snapshot().active_lease is None


async def test_runner_heartbeats_persistent_lease_while_process_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class RecordingCoordinator(GlobalCoordinator):
        heartbeat_count = 0

        def heartbeat(self, **kwargs):
            self.heartbeat_count += 1
            return super().heartbeat(**kwargs)

    script = tmp_path / "scenario.sh"
    script.write_text("#!/bin/sh\nsleep 0.08\n", encoding="utf-8")
    scenario = _runner_scenario(script)
    monkeypatch.setattr("app.runner.get_scenario", lambda _: scenario)
    coordinator = RecordingCoordinator(tmp_path / "state.json")
    runner = ScenarioRunner(
        tmp_path,
        tmp_path / "logs",
        coordinator=coordinator,
        lease_sec=1,
        heartbeat_interval_sec=0.01,
    )
    await runner.start(scenario.id, "run")
    assert runner._task is not None
    await runner._task
    assert coordinator.heartbeat_count >= 1
    assert coordinator.snapshot().active_lease is None


def _heartbeat_age(coordinator: GlobalCoordinator) -> float:
    lease = coordinator.snapshot().active_lease
    assert lease is not None
    return (datetime.now(timezone.utc) - lease.heartbeat_at).total_seconds()


async def test_lease_keeps_renewing_while_the_event_loop_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U1: a synchronous call holding the loop longer than the lease used to
    stop renewals, so the watchdog fenced the live run out (F63-R-run-17139f4c,
    2026-10-26: DIRTY right after injection). The renewal runs on its own
    thread now, so the lease stays current while the loop is held."""
    import time

    script = tmp_path / "scenario.sh"
    script.write_text("#!/bin/sh\nsleep 2\n", encoding="utf-8")
    scenario = _runner_scenario(script)
    monkeypatch.setattr("app.runner.get_scenario", lambda _: scenario)
    coordinator = GlobalCoordinator(tmp_path / "state.json")
    runner = ScenarioRunner(
        tmp_path, tmp_path / "logs", coordinator=coordinator,
        lease_sec=1, heartbeat_interval_sec=0.05,
    )
    await runner.start(scenario.id, "run")
    await asyncio.sleep(0.1)

    time.sleep(1.5)  # hold the event loop well past the 1s lease

    assert _heartbeat_age(coordinator) < 0.5
    decision = decide_watchdog(
        coordinator.snapshot(), now=datetime.now(timezone.utc), heartbeat_timeout_sec=1
    )
    assert decision.action == "healthy"
    assert runner._task is not None
    await runner._task
    finished = runner.get_current()
    assert finished is not None and finished.status == "succeeded" and finished.dirty is False


async def test_lease_keeps_renewing_while_the_default_thread_pool_is_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U1: renewals used to queue behind whatever filled asyncio's default pool
    (readiness self-checks of up to 120s each, probes, captures)."""
    import time
    from concurrent.futures import ThreadPoolExecutor

    loop = asyncio.get_running_loop()
    pool = ThreadPoolExecutor(max_workers=1)
    loop.set_default_executor(pool)
    script = tmp_path / "scenario.sh"
    script.write_text("#!/bin/sh\nsleep 2\n", encoding="utf-8")
    scenario = _runner_scenario(script)
    monkeypatch.setattr("app.runner.get_scenario", lambda _: scenario)
    coordinator = GlobalCoordinator(tmp_path / "state.json")
    runner = ScenarioRunner(
        tmp_path, tmp_path / "logs", coordinator=coordinator,
        lease_sec=1, heartbeat_interval_sec=0.05,
    )
    await runner.start(scenario.id, "run")
    hog = loop.run_in_executor(None, time.sleep, 1.6)  # the only pool worker

    await asyncio.sleep(1.4)

    assert _heartbeat_age(coordinator) < 0.5
    await hog
    assert runner._task is not None
    await runner._task
    pool.shutdown(wait=False)
    finished = runner.get_current()
    assert finished is not None and finished.status == "succeeded" and finished.dirty is False


async def test_watchdog_does_not_reclaim_a_lease_whose_run_is_live_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U1: an expired heartbeat on a run still executing in this process means
    the renewal was held up, not that the owner died. Claiming it fenced the
    live run out mid-injection and left the testbed DIRTY."""
    script = tmp_path / "scenario.sh"
    script.write_text("#!/bin/sh\nsleep 1.5\n", encoding="utf-8")
    scenario = _runner_scenario(script)
    monkeypatch.setattr("app.runner.get_scenario", lambda _: scenario)
    coordinator = GlobalCoordinator(tmp_path / "state.json")
    runner = ScenarioRunner(
        tmp_path, tmp_path / "logs", coordinator=coordinator,
        lease_sec=1, heartbeat_interval_sec=30,  # renewals effectively stalled
    )
    started = await runner.start(scenario.id, "run")
    await asyncio.sleep(1.1)
    stale = decide_watchdog(
        coordinator.snapshot(), now=datetime.now(timezone.utc), heartbeat_timeout_sec=1
    )
    assert stale.action == "claim_cleanup"

    decision = await runner.watchdog_service.tick()

    assert decision.action == "healthy"
    assert decision.reason == "lease_owner_running_in_process"
    assert decision.run_id == started.run_id
    assert coordinator.snapshot().cleanup_claim is None
    assert runner._task is not None
    await runner._task
    assert coordinator.snapshot().active_lease is None


async def test_watchdog_may_reclaim_once_the_run_stopped_renewing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review R4: the run stops its heartbeat before its closing steps. If one
    of those hangs, the lease expires; the watchdog must still be allowed to
    recover it, as before the in-process exemption."""
    from contextlib import suppress

    script = tmp_path / "scenario.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    scenario = _runner_scenario(script)
    monkeypatch.setattr("app.runner.get_scenario", lambda _: scenario)
    coordinator = GlobalCoordinator(tmp_path / "state.json")

    async def hanging_validator(_scenario):
        await asyncio.sleep(1.6)
        return True

    runner = ScenarioRunner(
        tmp_path, tmp_path / "logs", coordinator=coordinator,
        lease_sec=1, heartbeat_interval_sec=0.05, recovery_validator=hanging_validator,
    )
    started = await runner.start(scenario.id, "cleanup")
    await asyncio.sleep(1.3)  # script done, heartbeat stopped, validator hanging

    assert runner._task is not None and not runner._task.done()
    assert runner._owns_live_lease(started.run_id, started.fencing_token) is False
    stale = decide_watchdog(
        coordinator.snapshot(), now=datetime.now(timezone.utc), heartbeat_timeout_sec=1
    )
    assert stale.action == "claim_cleanup"
    with suppress(Exception):
        await runner._task


async def test_watchdog_still_reclaims_a_lease_no_live_run_here_owns(
    tmp_path: Path,
) -> None:
    """The exemption is only for a live in-process owner. A lease left by a
    previous process (or a finished run) is still claimed."""
    coordinator = GlobalCoordinator(tmp_path / "state.json")
    from app.production_runtime import RunArtifactStore

    runner = ScenarioRunner(
        tmp_path, tmp_path / "logs", coordinator=coordinator, lease_sec=1,
        artifact_store=RunArtifactStore(tmp_path / "runs"),
    )
    coordinator.acquire(
        run_id="orphan-run", scenario_id="F07-H",
        now=datetime.now(timezone.utc) - timedelta(minutes=5), lease_sec=1,
    )
    assert runner._owns_live_lease("orphan-run", 1) is False

    decision = await runner.watchdog_service.tick()

    # Claimed (then failed closed: the orphan has no capsule in this tmp dir).
    assert decision.action == "block_dirty"
    assert coordinator.snapshot().dirty_run is not None


async def test_two_runners_race_for_one_persistent_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "scenario.sh"
    script.write_text("#!/bin/sh\nsleep 0.1\n", encoding="utf-8")
    scenario = _runner_scenario(script)
    monkeypatch.setattr("app.runner.get_scenario", lambda _: scenario)
    state_path = tmp_path / "state.json"
    runners = [
        ScenarioRunner(tmp_path, tmp_path / f"logs-{index}", coordinator=GlobalCoordinator(state_path))
        for index in range(2)
    ]

    results = await asyncio.gather(
        *(runner.start(scenario.id, "run") for runner in runners),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    rejected = next(result for result in results if isinstance(result, Exception))
    assert "active lease" in str(rejected)
    winner = next(runner for runner in runners if runner._task is not None)
    await winner._task


async def test_restart_observes_existing_lease_before_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "scenario.sh"
    script.write_text("#!/bin/sh\necho must-not-run > should-not-exist\n", encoding="utf-8")
    scenario = _runner_scenario(script)
    monkeypatch.setattr("app.runner.get_scenario", lambda _: scenario)
    state_path = tmp_path / "state.json"
    GlobalCoordinator(state_path).acquire(
        run_id="pre-restart-run",
        scenario_id=scenario.id,
        now=datetime.now(timezone.utc),
        lease_sec=60,
    )
    restarted = ScenarioRunner(tmp_path, tmp_path / "logs", coordinator=GlobalCoordinator(state_path))
    with pytest.raises(RuntimeError, match="active lease"):
        await restarted.start(scenario.id, "run")
    assert not (tmp_path / "should-not-exist").exists()
    assert not (tmp_path / "logs").exists()


# --- U10: a runner restart leaves no lease or cleanup claim behind -----------
#
# "Previous runner process" is a real child process here: it writes through its
# own coordinator (its own owner and marker) and exits, so the kernel drops its
# flock exactly as a container stop or crash does.

_BACKEND_ROOT = Path(__file__).resolve().parents[1]

_PREVIOUS_PROCESS = r"""
import sys, time
from datetime import datetime, timezone
from pathlib import Path
from app.coordinator import GlobalCoordinator

state_path, steps, linger = Path(sys.argv[1]), sys.argv[2].split(","), sys.argv[3] == "linger"
coordinator = GlobalCoordinator(state_path)
now = datetime.now(timezone.utc)
if "alive_only" in steps:
    coordinator.hold_owner_marker()
else:
    lease = coordinator.acquire(run_id="run-before", scenario_id="F07-H", now=now, lease_sec=600)
if "dirty" in steps:
    coordinator.mark_dirty(run_id="run-before", fencing_token=lease.fencing_token, reason="cleanup_failed", now=now)
if "claim" in steps:
    coordinator.claim_cleanup(run_id="run-before", fencing_token=lease.fencing_token, claimant="manual-cleanup-cut", now=now)
if "watchdog_claim" in steps:
    coordinator.claim_cleanup(run_id="run-before", fencing_token=lease.fencing_token, claimant="watchdog-cut", now=now)
print(coordinator.owner, flush=True)
if linger:
    time.sleep(120)
"""


def _previous_process(state_path: Path, *steps: str, linger: bool = False):
    import subprocess
    import sys

    process = subprocess.Popen(
        [
            sys.executable, "-c", _PREVIOUS_PROCESS, str(state_path),
            ",".join(steps) or "-", "linger" if linger else "exit",
        ],
        cwd=_BACKEND_ROOT,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    owner = process.stdout.readline().strip()
    assert owner, "previous runner process did not start"
    if not linger:
        assert process.wait(timeout=30) == 0
    return process, owner


def _recover(state_path: Path, runs_root: Path):
    from app.restart_recovery import recover_after_restart

    return recover_after_restart(
        GlobalCoordinator(state_path), runs_root, now=datetime.now(timezone.utc)
    )


def test_restart_recovery_marks_the_run_cut_mid_way_dirty(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "coordinator.json"
    runs_root = tmp_path / "runs"
    (runs_root / "run-before").mkdir(parents=True)
    _, previous_owner = _previous_process(state_path)
    assert GlobalCoordinator(state_path).snapshot().active_lease.owner == previous_owner

    recovered = _recover(state_path, runs_root)

    assert [(item.action, item.run_id, item.reason) for item in recovered] == [
        ("marked_dirty", "run-before", "runner_restarted_mid_run")
    ]
    state = GlobalCoordinator(state_path).snapshot()
    assert state.active_lease is None and state.cleanup_claim is None
    assert state.dirty_run is not None
    assert (state.dirty_run.run_id, state.dirty_run.fencing_token) == ("run-before", 1)
    assert state.dirty_run.reason == "runner_restarted_mid_run"
    record = json.loads((runs_root / "run-before" / "restart-recovery.json").read_text())
    assert record["action"] == "marked_dirty"
    assert record["previous_owner"] == previous_owner
    # No cleanup ran, so none is reported.
    assert not (runs_root / "run-before" / "cleanup.json").exists()
    # It is now an ordinary DIRTY run: it blocks starts and takes a cleanup.
    now = datetime.now(timezone.utc)
    assert decide_watchdog(state, now=now, heartbeat_timeout_sec=30).action == "block_dirty"
    coordinator = GlobalCoordinator(state_path)
    with pytest.raises(RuntimeError, match="dirty run"):
        coordinator.acquire(run_id="run-after", scenario_id="F07-H", now=now, lease_sec=60)
    claim = coordinator.claim_cleanup(
        run_id="run-before", fencing_token=1, claimant="manual-cleanup-next", now=now
    )
    assert claim.owner == coordinator.owner
    # The previous process's marker is swept once it is found dead.
    assert not (state_path.parent / "coordinator.json.owners" / f"{previous_owner}.lock").exists()


def test_restart_recovery_releases_a_cut_cleanup_claim_and_keeps_dirty(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "coordinator.json"
    runs_root = tmp_path / "runs"
    (runs_root / "run-before").mkdir(parents=True)
    _previous_process(state_path, "dirty", "claim")
    before = GlobalCoordinator(state_path).snapshot()
    assert before.dirty_run is not None and before.cleanup_claim is not None
    now = datetime.now(timezone.utc)
    assert decide_watchdog(before, now=now, heartbeat_timeout_sec=30).action == "cleanup_claimed"

    recovered = _recover(state_path, runs_root)

    assert [(item.action, item.claimant) for item in recovered] == [
        ("released_cleanup_claim", "manual-cleanup-cut")
    ]
    state = GlobalCoordinator(state_path).snapshot()
    assert state.cleanup_claim is None
    assert state.dirty_run == before.dirty_run
    cleanup = json.loads((runs_root / "run-before" / "cleanup.json").read_text())
    assert cleanup["succeeded"] is False and cleanup["source"] == "restart-recovery"
    assert "manual-cleanup-cut" in cleanup["reason"] and cleanup["finished_at"]
    # The next cleanup can claim it again.
    GlobalCoordinator(state_path).claim_cleanup(
        run_id="run-before", fencing_token=1, claimant="manual-cleanup-next", now=now
    )


def test_restart_recovery_keeps_a_cleanup_success_already_recorded(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "coordinator.json"
    run_dir = tmp_path / "runs" / "run-before"
    run_dir.mkdir(parents=True)
    _previous_process(state_path, "dirty", "claim")
    succeeded = {"schema_version": 1, "source": "manual-dirty-cleanup", "succeeded": True,
                 "effect_ended_at": "2026-11-09T00:00:00+00:00"}
    (run_dir / "cleanup.json").write_text(json.dumps(succeeded))

    _recover(state_path, tmp_path / "runs")

    # Cut between the successful cleanup and clear_dirty: stays DIRTY for the
    # next cleanup, but the success it recorded is not overwritten.
    state = GlobalCoordinator(state_path).snapshot()
    assert state.dirty_run is not None and state.cleanup_claim is None
    assert json.loads((run_dir / "cleanup.json").read_text()) == succeeded


def test_restart_recovery_marks_a_lease_whose_cleanup_was_cut_dirty(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "coordinator.json"
    runs_root = tmp_path / "runs"
    (runs_root / "run-before").mkdir(parents=True)
    _previous_process(state_path, "watchdog_claim")

    recovered = _recover(state_path, runs_root)

    assert [(item.action, item.reason, item.claimant) for item in recovered] == [
        ("marked_dirty", "runner_restarted_during_cleanup", "watchdog-cut")
    ]
    state = GlobalCoordinator(state_path).snapshot()
    assert state.active_lease is None and state.cleanup_claim is None
    assert state.dirty_run is not None
    assert state.dirty_run.reason == "runner_restarted_during_cleanup"
    cleanup = json.loads((runs_root / "run-before" / "cleanup.json").read_text())
    assert cleanup["succeeded"] is False


def test_restart_recovery_leaves_a_live_runner_process_alone(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "coordinator.json"
    runs_root = tmp_path / "runs"
    for steps in [(), ("dirty", "claim")]:
        if state_path.exists():
            state_path.unlink()
        process, _ = _previous_process(state_path, *steps, linger=True)
        try:
            before = GlobalCoordinator(state_path).snapshot()
            assert _recover(state_path, runs_root) == []
            assert GlobalCoordinator(state_path).snapshot() == before
        finally:
            process.kill()
            process.wait(timeout=30)
        # Once that process is gone the same state is taken back.
        assert _recover(state_path, runs_root) != []
        state = GlobalCoordinator(state_path).snapshot()
        assert state.active_lease is None and state.cleanup_claim is None


def test_restart_recovery_takes_nothing_from_a_run_live_in_this_process(tmp_path: Path) -> None:
    """No restart: this process holds the lease (a run is executing) or a
    cleanup claim (a cleanup is running). Nothing is recovered."""
    state_path = tmp_path / "coordinator.json"
    coordinator = GlobalCoordinator(state_path)
    now = datetime.now(timezone.utc)
    assert _recover(state_path, tmp_path / "runs") == []  # empty coordinator
    lease = coordinator.acquire(run_id="run-live", scenario_id="F07-H", now=now, lease_sec=60)
    before = coordinator.snapshot()
    assert _recover(state_path, tmp_path / "runs") == []
    assert coordinator.snapshot() == before
    coordinator.mark_dirty(run_id="run-live", fencing_token=lease.fencing_token, reason="x", now=now)
    coordinator.claim_cleanup(
        run_id="run-live", fencing_token=lease.fencing_token, claimant="manual-cleanup-live", now=now
    )
    before = coordinator.snapshot()
    assert _recover(state_path, tmp_path / "runs") == []
    assert coordinator.snapshot() == before


def test_restart_recovery_sweeps_markers_of_gone_processes(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "coordinator.json"
    owners = state_path.parent / "coordinator.json.owners"
    _, gone_owner = _previous_process(state_path, "alive_only")
    assert (owners / f"{gone_owner}.lock").exists()
    assert _recover(state_path, tmp_path / "runs") == []
    assert [marker.stem for marker in owners.glob("*.lock")] == [GlobalCoordinator(state_path).owner]


def _legacy_state(state_path: Path, *, claim: bool) -> None:
    """State as a runner built before owners were recorded left it."""
    now = datetime.now(timezone.utc).isoformat()
    document = {
        "next_fencing_token": 2,
        "active_lease": {
            "run_id": "run-old", "scenario_id": "F07-H", "fencing_token": 1,
            "acquired_at": now, "heartbeat_at": now, "expires_at": now,
        },
        "dirty_run": None,
        "cleanup_claim": (
            {"run_id": "run-old", "fencing_token": 1, "claimant": "watchdog-old", "claimed_at": now}
            if claim else None
        ),
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(document))


def test_restart_recovery_takes_back_owner_less_state_only_when_no_other_runner_lives(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state" / "coordinator.json"
    runs_root = tmp_path / "runs"
    _legacy_state(state_path, claim=True)
    process, _ = _previous_process(state_path, "alive_only", linger=True)
    try:
        # Another runner lives on this state: an owner-less entry cannot be
        # told apart from it, so it is left alone.
        before = GlobalCoordinator(state_path).snapshot()
        assert _recover(state_path, runs_root) == []
        assert GlobalCoordinator(state_path).snapshot() == before
    finally:
        process.kill()
        process.wait(timeout=30)

    recovered = _recover(state_path, runs_root)

    assert [(item.action, item.reason, item.previous_owner) for item in recovered] == [
        ("marked_dirty", "runner_restarted_during_cleanup", None)
    ]
    state = GlobalCoordinator(state_path).snapshot()
    assert state.active_lease is None and state.cleanup_claim is None
    assert state.dirty_run is not None and state.dirty_run.run_id == "run-old"


async def test_runner_startup_recovers_before_the_watchdog_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import live_queue as live_queue_module
    from app import runner as runner_module
    from app.main import app, lifespan

    state_dir = tmp_path / "state"
    monkeypatch.setenv("COORDINATOR_STATE_PATH", str(state_dir / "coordinator.json"))
    monkeypatch.setenv("CAPTURE_STATE_PATH", str(state_dir / "capture-jobs.json"))
    monkeypatch.setenv("LIVE_QUEUE_STATE_PATH", str(state_dir / "live-queue.json"))
    monkeypatch.setenv("TRUSTED_RUNS_ROOT", str(tmp_path / "runs"))
    (tmp_path / "runs" / "run-before").mkdir(parents=True)
    _previous_process(state_dir / "coordinator.json", "dirty", "claim")
    runner_module.reset_runner_for_tests(script_dir=tmp_path, log_dir=tmp_path / "logs")
    monkeypatch.setattr(live_queue_module, "_queue", None)

    async with lifespan(app):
        state = runner_module.get_runner().coordinator.snapshot()

    assert state.cleanup_claim is None
    assert state.dirty_run is not None and state.dirty_run.run_id == "run-before"
    assert (tmp_path / "runs" / "run-before" / "restart-recovery.json").is_file()


async def test_failure_attempts_cleanup_once_then_dirty_blocks_until_manual_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counter = tmp_path / "cleanup-count"
    allow_cleanup = tmp_path / "allow-cleanup"
    script = tmp_path / "scenario.sh"
    script.write_text(
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = cleanup ]; then\n"
        f"  echo cleanup >> {counter}\n"
        f"  [ -f {allow_cleanup} ] && exit 0\n"
        "  exit 3\n"
        "fi\n"
        "exit 2\n",
        encoding="utf-8",
    )
    controller = parse_controller(_controller_yaml(mode="evaluation"))
    scenario = _runner_scenario(script, controller=controller)
    monkeypatch.setattr("app.runner.get_scenario", lambda _: scenario)
    state_path = tmp_path / "state.json"
    first = ScenarioRunner(
        tmp_path,
        tmp_path / "logs-1",
        coordinator=GlobalCoordinator(state_path),
        recovery_validator=lambda _: False,
    )
    await first.start(scenario.id, "run")
    assert first._task is not None
    await first._task
    assert counter.read_text(encoding="utf-8").splitlines() == ["cleanup"]
    assert first.get_current().dirty is True
    dirty = GlobalCoordinator(state_path).snapshot().dirty_run
    assert dirty is not None

    blocked = ScenarioRunner(tmp_path, tmp_path / "logs-2", coordinator=GlobalCoordinator(state_path))
    with pytest.raises(RuntimeError, match="dirty run"):
        await blocked.start(scenario.id, "run")

    allow_cleanup.touch()
    recovery = ScenarioRunner(
        tmp_path,
        tmp_path / "logs-3",
        coordinator=GlobalCoordinator(state_path),
        recovery_validator=lambda _: True,
    )
    manual = await recovery.start(scenario.id, "cleanup")
    assert manual.fencing_token == dirty.fencing_token
    assert recovery._task is not None
    await recovery._task
    assert recovery.get_current().status == "succeeded"
    state = GlobalCoordinator(state_path).snapshot()
    assert state.dirty_run is None and state.cleanup_claim is None
    assert counter.read_text(encoding="utf-8").splitlines() == ["cleanup", "cleanup"]


async def test_controller_manual_cleanup_keeps_dirty_when_recovery_is_not_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "scenario.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    controller = parse_controller(_controller_yaml(mode="evaluation"))
    scenario = _runner_scenario(script, controller=controller)
    monkeypatch.setattr("app.runner.get_scenario", lambda _: scenario)
    coordinator = GlobalCoordinator(tmp_path / "state.json")
    lease = coordinator.acquire(
        run_id="dirty-source",
        scenario_id=scenario.id,
        now=datetime.now(timezone.utc),
        lease_sec=60,
    )
    coordinator.mark_dirty(
        run_id=lease.run_id,
        fencing_token=lease.fencing_token,
        reason="test",
        now=datetime.now(timezone.utc),
    )
    runner = ScenarioRunner(
        tmp_path,
        tmp_path / "logs",
        coordinator=coordinator,
        recovery_validator=lambda _: False,
    )
    await runner.start(scenario.id, "cleanup")
    assert runner._task is not None
    await runner._task
    assert runner.get_current().status == "failed"
    assert runner.get_current().dirty is True
    assert coordinator.snapshot().dirty_run is not None
    assert coordinator.snapshot().cleanup_claim is None


def test_dry_run_normalizes_canonical_kube_path_and_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "scenario.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    scenario = _runner_scenario(script)
    monkeypatch.setattr("app.runner.get_scenario", lambda _: scenario)
    monkeypatch.delenv("KUBECONFIG", raising=False)
    monkeypatch.delenv("KUBE_CONTEXT", raising=False)
    plan = ScenarioRunner(tmp_path, tmp_path / "logs").dry_run(scenario.id)
    preflight = plan["kubernetes_preflight"]
    assert preflight["execution"] == "planned_only"
    assert preflight["cluster_contact"] is False
    assert all(check["passed"] for check in preflight["checks"])
    assert preflight["checks"][0]["expected"] == "/root/tb-kubeconfig"
    assert preflight["checks"][1]["expected"] == "kubernetes-admin@kubernetes"

    monkeypatch.setenv("KUBECONFIG", "/home/nkia/.kube/config")
    invalid = ScenarioRunner(tmp_path, tmp_path / "logs").dry_run(scenario.id)
    assert invalid["valid"] is False
    assert invalid["kubernetes_preflight"]["checks"][0]["passed"] is False
