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
