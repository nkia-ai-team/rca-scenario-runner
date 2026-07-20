from __future__ import annotations

import json
from types import SimpleNamespace
from datetime import datetime, timezone

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
    assert runner.get_current().status == "succeeded"


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
