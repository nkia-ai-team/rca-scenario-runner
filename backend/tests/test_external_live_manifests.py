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
    guarded = {
        "prometheus": probes._prometheus_observation,
        "clickhouse": probes._clickhouse_observation,
        "database": probes._database_observation,
    }

    problems = []
    for scenario_id in sorted(ready & set(controllers)):
        for observation in controllers[scenario_id]["observations"]:
            probe = guarded.get(observation.get("adapter"))
            if probe is None:
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
