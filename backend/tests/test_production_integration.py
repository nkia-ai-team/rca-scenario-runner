from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.adaptive import ControllerPhase
from app.adaptive_runtime import ApplyRequest, CleanupRequest, EligibilityRequest
from app.capture_orchestration import CaptureRequest, CaptureScheduler, ScenarioMetadata
from app import live_probes
from app.live_probes import (
    BASELINE_PAID_ORDERS_SQL,
    BLOCKED_SESSION_SQL,
    INDEX_PRESENT_CONTRACT,
    INDEX_PRESENT_SQL,
    PAYMENT_DUPLICATE_SINCE_T1_SQL,
    TAGGED_SESSION_SQL,
)
from app.observations import ApprovedQueryRegistry
from app.production_runtime import (
    PARAMETERLESS_DATABASE_PROBES,
    ProductionCaptureInvoker,
    RunArtifactStore,
    TrustedDispatcherApplier,
    _configured_live_probes,
    file_sha256,
)
from app.coordinator import GlobalCoordinator
from app.runner import ScenarioRunner


NOW = datetime(2026, 7, 16, 11, 5, tzinfo=timezone.utc)


class Clock:
    def now(self) -> datetime:
        return NOW


def scenario_metadata() -> ScenarioMetadata:
    return ScenarioMetadata(
        title="North-south surge",
        description="A bounded user traffic surge exercises the checkout path.",
        cause="North-south traffic surge",
        injection_summary="Inject approved k6 traffic through the public NodePort.",
        user_impact="Checkout latency and errors increase.",
        distinguishing_evidence="Ingress and internal call volume rise proportionally.",
    )


class PlainTextResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return b"OK"


def _completed(document: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, json.dumps(document), "")


def test_plain_text_health_response_is_accepted(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: PlainTextResponse())
    probes = _configured_live_probes(
        run_id="run-health",
        scenario_id="F01-R",
        clock=Clock(),
        process_runner=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )
    probes.paths = probes.paths.__class__(
        coordinator=tmp_path / "coordinator.json",
        runs=tmp_path / "runs",
        baseline_status=tmp_path / "baseline.json",
        loadgen_summary=tmp_path / "loadgen.json",
        capture_root=tmp_path / "runs",
    )
    evidence = probes.inspect(
        EligibilityRequest(
            run_id="run-health",
            scenario_id="F01-R",
            checks=["target-health"],
            clean_window_sec=7200,
            requested_at=NOW,
        )
    )
    assert evidence.check_results["target-health"] is True


def test_production_index_probe_executes_only_fixed_parameterized_contract(monkeypatch) -> None:
    calls: list[tuple[list[str], dict]] = []

    def process(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, "1\n", "")

    monkeypatch.setenv("COMMERCE_DB_PASSWORD", "secret")
    probes = _configured_live_probes(
        run_id="run-index",
        scenario_id="F02-R",
        clock=Clock(),
        process_runner=process,
    )
    query = ApprovedQueryRegistry.from_path().bind(
        {"query_id": "database.index_present", "parameters": INDEX_PRESENT_CONTRACT}
    )

    observed = probes.observe(query)

    assert observed["quality"] == "good" and observed["value"] is True
    argv, kwargs = calls[0]
    assert argv == ["psql", "-At", "-c", INDEX_PRESENT_SQL]
    assert kwargs["env"]["PGHOST"] == "192.168.122.77"
    assert kwargs["env"]["PGPORT"] == "30432"
    assert kwargs["env"]["PGOPTIONS"] == (
        "-c lucida.index_schema=product_schema "
        "-c lucida.index_table=products "
        "-c lucida.index_name=idx_products_name"
    )


def test_every_probe_sql_live_probes_issues_is_approved_by_the_dispatch() -> None:
    """test_live_probes fakes database_client, so the approved-SQL dispatch is never
    exercised there — which is how five probes shipped dead. Five of eight were
    unreachable on 2026-07-30, baseline-business-success (required by 44 of 44 live
    scenarios) among them. Compare the two sets directly instead."""
    issued = {
        name: value
        for name, value in vars(live_probes).items()
        if name.endswith("_SQL") and isinstance(value, str)
    }
    approved = set(PARAMETERLESS_DATABASE_PROBES) | {
        TAGGED_SESSION_SQL,
        BLOCKED_SESSION_SQL,
        INDEX_PRESENT_SQL,
        PAYMENT_DUPLICATE_SINCE_T1_SQL,
    }

    unapproved = sorted(name for name, sql in issued.items() if sql not in approved)

    assert unapproved == []


def test_baseline_business_success_reaches_psql_and_reads_the_paid_order_count(
    monkeypatch, tmp_path
) -> None:
    """The refusal surfaced as a bare False through _safe_bool, so the check read as
    "checkout is broken" while commerce was serving 192 PAID orders per five minutes
    against a threshold of 5."""
    calls: list[tuple[list[str], dict]] = []

    def process(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, "192\n", "")

    monkeypatch.setenv("COMMERCE_DB_PASSWORD", "secret")
    probes = _configured_live_probes(
        run_id="run-baseline",
        scenario_id="F08-P",
        clock=Clock(),
        process_runner=process,
    )
    probes.paths = probes.paths.__class__(
        coordinator=tmp_path / "coordinator.json",
        runs=tmp_path / "runs",
        baseline_status=tmp_path / "baseline.json",
        loadgen_summary=tmp_path / "loadgen.json",
        capture_root=tmp_path / "runs",
    )

    evidence = probes.inspect(
        EligibilityRequest(
            run_id="run-baseline",
            scenario_id="F08-P",
            checks=["baseline-business-success"],
            clean_window_sec=1800,
            requested_at=NOW,
        )
    )

    assert evidence.check_results["baseline-business-success"] is True
    psql_calls = [(argv, kwargs) for argv, kwargs in calls if argv[0] == "psql"]
    argv, kwargs = psql_calls[0]
    assert argv == ["psql", "-At", "-c", BASELINE_PAID_ORDERS_SQL]
    assert kwargs["env"]["PGDATABASE"] == "commerce"
    assert kwargs["env"]["PGOPTIONS"] == ""


def test_profile_control_uses_scenario_id_confirmation_and_composite_order(tmp_path) -> None:
    profile_control = tmp_path / "profile-control.py"
    profile_control.write_text("trusted boundary", encoding="utf-8")
    calls: list[list[str]] = []
    plan = {
        "live_allowed": True,
        "scenario": {"id": "F08-H", "slug": "f08-h-composite-overlap"},
        "plan_digest": "a" * 64,
        "profile_instances": [
            {"profile_id": "mock.expectation", "parameters": {"status": 429}},
            {
                "profile_id": "load.north_south",
                "parameters": {"target_rps": 80},
                "approved_levels": [
                    {"level_id": "low", "parameters": {"target_rps": 60}}
                ],
            },
        ],
    }

    def process(argv, **_kwargs):
        calls.append(argv)
        if "--plan" in argv:
            return _completed({"normalized_plan": plan})
        if argv[argv.index("--action") + 1] == "apply":
            profile = argv[argv.index("--profile") + 1]
            return _completed(
                {
                    "applied_at": (
                        "2026-07-16T11:04:59Z"
                        if profile == "mock.expectation"
                        else "2026-07-16T11:05:00Z"
                    )
                }
            )
        return _completed(
            {
                "succeeded": True,
                "effect_ended_at": "2026-07-16T11:05:00Z",
                "reason": None,
            }
        )

    applier = TrustedDispatcherApplier(
        "f08-h-composite-overlap",
        primary_profile="load.north_south",
        companion_profiles=["mock.expectation"],
        dispatcher=tmp_path / "run-scenario.sh",
        profile_control=profile_control,
        process_runner=process,
        clock=Clock(),
    )
    applied = applier.apply(
        ApplyRequest(
            run_id="run-1",
            scenario_id="F08-H",
            fencing_token=7,
            profile_id="load.north_south",
            level_index=0,
            level_id="low",
            parameters={"target_rps": 60},
            idempotency_key="apply-key",
            requested_at=NOW,
        )
    )
    assert applied.applied_at == datetime(2026, 7, 16, 11, 4, 59, tzinfo=timezone.utc)
    apply_calls = [call for call in calls if "apply" in call]
    assert [call[call.index("--profile") + 1] for call in apply_calls] == [
        "mock.expectation",
        "load.north_south",
    ]
    assert "--level-index" not in apply_calls[0]
    assert "--parameters-json" not in apply_calls[0]
    assert apply_calls[0][apply_calls[0].index("--idempotency-key") + 1].endswith(
        ":0:mock.expectation"
    )
    assert apply_calls[1][apply_calls[1].index("--level-id") + 1] == "low"
    for call in apply_calls:
        assert call[call.index("--confirm") + 1] == f"LIVE:F08-H:{'a' * 64}"

    cleaned = applier.cleanup(
        CleanupRequest(
            run_id="run-1",
            scenario_id="F08-H",
            fencing_token=7,
            profile_id="load.north_south",
            idempotency_key="cleanup-key",
            requested_at=NOW,
        )
    )
    assert cleaned.succeeded
    cleanup_calls = [call for call in calls if "cleanup" in call]
    assert [call[call.index("--profile") + 1] for call in cleanup_calls] == [
        "load.north_south",
        "mock.expectation",
    ]


def test_profile_control_absence_fails_before_live_effect(tmp_path) -> None:
    calls = []
    plan = {
        "live_allowed": True,
        "scenario": {"id": "F07-H", "slug": "f07-h-north-south-surge"},
        "plan_digest": "a" * 64,
        "profile_instances": [
            {"profile_id": "load.north_south", "parameters": {"target_rps": 80}}
        ],
    }

    def process(argv, **_kwargs):
        calls.append(argv)
        return _completed({"normalized_plan": plan})

    applier = TrustedDispatcherApplier(
        "f07-h-north-south-surge",
        primary_profile="load.north_south",
        dispatcher=tmp_path / "run-scenario.sh",
        profile_control=tmp_path / "missing.py",
        process_runner=process,
    )
    with pytest.raises(RuntimeError, match="per-profile control API"):
        applier.apply(
            ApplyRequest(
                run_id="run-1",
                scenario_id="F07-H",
                fencing_token=1,
                profile_id="load.north_south",
                level_index=0,
                level_id="fixed",
                parameters={"target_rps": 80},
                idempotency_key="key",
                requested_at=NOW,
            )
        )
    assert len(calls) == 1 and "--plan" in calls[0]


def test_failed_cleanup_records_the_dispatcher_stderr_not_just_its_class(tmp_path) -> None:
    """cleanup.json is the only trace a dirty run leaves of why recovery failed.

    A bare "trusted_dispatcher:CalledProcessError" cannot be acted on — F01-R run
    0104bd01 (2026-07-31) stopped the 44-scenario batch and the operator had to
    re-issue the call by hand to see anything at all.
    """
    plan = {
        "live_allowed": True,
        "scenario": {"id": "F07-H", "slug": "f07-h-north-south-surge"},
        "plan_digest": "a" * 64,
        "profile_instances": [
            {"profile_id": "load.north_south", "parameters": {"target_rps": 80}}
        ],
    }
    profile_control = tmp_path / "profile-control.py"
    profile_control.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    def process(argv, **_kwargs):
        if "--plan" in argv:
            return _completed({"normalized_plan": plan})
        raise subprocess.CalledProcessError(
            1, argv, output="", stderr="kubectl wait timed out\nwaiting for pod/lock-holder\n"
        )

    applier = TrustedDispatcherApplier(
        "f07-h-north-south-surge",
        primary_profile="load.north_south",
        dispatcher=tmp_path / "run-scenario.sh",
        profile_control=profile_control,
        process_runner=process,
    )
    result = applier.cleanup(
        CleanupRequest(
            run_id="run-1",
            scenario_id="F07-H",
            fencing_token=1,
            profile_id="load.north_south",
            idempotency_key="cleanup-key",
            requested_at=NOW,
        )
    )
    assert result.succeeded is False
    assert "CalledProcessError" in result.reason
    assert "kubectl wait timed out" in result.reason
    # collapsed to one line so it stays readable inside a reason string
    assert "\n" not in result.reason


def test_a_failing_primary_cleanup_still_undoes_the_companion(tmp_path) -> None:
    """One un-undoable effect must not leave the others injected.

    F01-R run 0104bd01 (2026-07-31): db.lock cleanup failed first in the order,
    so the companion surge was never stopped and ran its full 15m. The next
    attempt's preflight refused on the still-tagged k6 — one failed cleanup cost
    two scenarios. The profiles are independent effects.
    """
    plan = {
        "live_allowed": True,
        "scenario": {"id": "F01-R", "slug": "f01-r-pg-lock-checkout"},
        "plan_digest": "a" * 64,
        "profile_instances": [
            {"profile_id": "db.lock", "parameters": {"hold_seconds": 600}},
            {"profile_id": "load.north_south", "parameters": {"target_rps": 35}},
        ],
    }
    profile_control = tmp_path / "profile-control.py"
    profile_control.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    cleaned: list[str] = []

    def process(argv, **_kwargs):
        if "--plan" in argv:
            return _completed({"normalized_plan": plan})
        profile = argv[argv.index("--profile") + 1]
        cleaned.append(profile)
        if profile == "db.lock":
            raise subprocess.CalledProcessError(1, argv, output="", stderr="lock pod stuck")
        return _completed(
            {"succeeded": True, "effect_ended_at": "2026-07-16T11:05:00Z", "reason": None}
        )

    applier = TrustedDispatcherApplier(
        "f01-r-pg-lock-checkout",
        primary_profile="db.lock",
        companion_profiles=["load.north_south"],
        dispatcher=tmp_path / "run-scenario.sh",
        profile_control=profile_control,
        process_runner=process,
    )
    result = applier.cleanup(
        CleanupRequest(
            run_id="run-1",
            scenario_id="F01-R",
            fencing_token=1,
            profile_id="db.lock",
            idempotency_key="cleanup-key",
            requested_at=NOW,
        )
    )
    assert cleaned == ["db.lock", "load.north_south"]
    # still fails closed, and names the effect that survived
    assert result.succeeded is False
    assert "db.lock" in result.reason and "lock pod stuck" in result.reason


def test_trusted_run_artifacts_are_atomic_restricted_and_hashable(tmp_path) -> None:
    store = RunArtifactStore(tmp_path / "runs")
    run_dir = store.create("run-001", {"scenario_id": "F07-H", "controller": {}})
    store.persist_session(
        run_dir,
        {
            "controller_state": {"phase": "succeeded"},
            "level_changes": [],
            "cleanup": {"succeeded": True},
            "recovery": {"status": "succeeded"},
        },
    )
    result = store.write_result(run_dir, {"mode": "calibration", "dirty": False})

    assert (os.stat(run_dir).st_mode & 0o777) == 0o750
    for path in run_dir.iterdir():
        assert (os.stat(path).st_mode & 0o022) == 0
    assert len(file_sha256(run_dir / "plan.json")) == 64
    assert json.loads(result.read_text())["dirty"] is False


def _capsule_contract(tmp_path, plan):
    root = tmp_path / "contracts"
    root.mkdir()
    dispatcher = root / "run-scenario.sh"
    dispatcher.write_text(
        "#!/bin/sh\nprintf '%s\\n' '" + json.dumps({"normalized_plan": plan}) + "'\n",
        encoding="utf-8",
    )
    dispatcher.chmod(0o755)
    control = root / "profile-control.py"
    control.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    control.chmod(0o755)
    executor = root / "profiles" / "load.py"
    executor.parent.mkdir()
    executor.write_text("#!/bin/sh\n", encoding="utf-8")
    executor.chmod(0o755)
    return root


def test_recovery_capsule_is_contained_hashed_mode_locked_and_tamper_evident(tmp_path) -> None:
    plan = {
        "scenario": {"id": "F07-H", "slug": "adaptive"},
        "live_allowed": True,
        "plan_digest": "a" * 64,
        "profile_instances": [],
    }
    contract = _capsule_contract(tmp_path, plan)
    store = RunArtifactStore(tmp_path / "runs")
    run_dir, compiled = store.prepare_capsule(
        "run-capsule",
        contract_root=contract,
        scenario_slug="adaptive",
        binding={
            "catalog_slug": "adaptive", "primary_profile": "load.north_south",
            "logical_profile_id": "load.north_south", "companion_profiles": [],
        },
    )
    assert compiled == plan and store.verify_capsule(run_dir)["scenario_slug"] == "adaptive"
    assert (run_dir / "capsule" / "contracts" / "run-scenario.sh").stat().st_mode & 0o777 == 0o750
    (run_dir / "capsule" / "contracts" / "profiles" / "load.py").write_text("tampered")
    with pytest.raises(RuntimeError, match="hash mismatch"):
        store.verify_capsule(run_dir)

    outside = tmp_path / "outside"
    outside.write_text("secret")
    (contract / "escape").symlink_to(outside)
    with pytest.raises(RuntimeError, match="link or special"):
        store.prepare_capsule(
            "run-link", contract_root=contract, scenario_slug="adaptive", binding={}
        )


def test_capsule_survives_source_contract_drift_and_wal_records_apply_crash(tmp_path) -> None:
    plan = {
        "scenario": {"id": "F07-H", "slug": "adaptive"},
        "live_allowed": True,
        "plan_digest": "a" * 64,
        "profile_instances": [
            {
                "profile_id": "load.north_south", "parameters": {"target_rps": 60},
                "approved_levels": [{"level_id": "low", "parameters": {"target_rps": 60}}],
            }
        ],
    }
    contract = _capsule_contract(tmp_path, plan)
    store = RunArtifactStore(tmp_path / "runs")
    run_dir, _ = store.prepare_capsule(
        "run-wal", contract_root=contract, scenario_slug="adaptive",
        binding={
            "catalog_slug": "adaptive", "primary_profile": "load.north_south",
            "logical_profile_id": "load.north_south", "companion_profiles": [],
        },
    )
    contract.joinpath("run-scenario.sh").write_text("drifted", encoding="utf-8")
    capsule_dispatcher = run_dir / "capsule" / "contracts" / "run-scenario.sh"
    capsule_control = run_dir / "capsule" / "contracts" / "profile-control.py"

    def crash_after_intent(argv, **kwargs):
        if argv[0] == str(capsule_dispatcher):
            return subprocess.run(argv, **kwargs)
        raise RuntimeError("simulated process death after fsynced intent")

    applier = TrustedDispatcherApplier(
        "adaptive", primary_profile="load.north_south",
        dispatcher=capsule_dispatcher, profile_control=capsule_control,
        process_runner=crash_after_intent, run_dir=run_dir,
    )
    with pytest.raises(RuntimeError, match="simulated process death"):
        applier.apply(
            ApplyRequest(
                run_id="run-wal", scenario_id="F07-H", fencing_token=3,
                profile_id="load.north_south", level_index=0, level_id="low",
                parameters={"target_rps": 60}, idempotency_key="apply:0", requested_at=NOW,
            )
        )
    operations = json.loads((run_dir / "mutations.json").read_text())["operations"]
    assert operations == [
        {
            "action": "apply", "complete_at": None, "idempotency_key": "apply:0",
            "intent_at": operations[0]["intent_at"], "profile_id": "load.north_south",
        }
    ]

    control_calls = []

    def recover(argv, **kwargs):
        if argv[0] == str(capsule_dispatcher):
            return subprocess.run(argv, **kwargs)
        control_calls.append(argv)
        return _completed(
            {"succeeded": True, "effect_ended_at": "2026-07-16T11:05:00Z", "reason": None}
        )

    recovered = TrustedDispatcherApplier(
        "adaptive", primary_profile="load.north_south",
        dispatcher=capsule_dispatcher, profile_control=capsule_control,
        process_runner=recover, run_dir=run_dir,
    ).cleanup(
        CleanupRequest(
            run_id="run-wal", scenario_id="F07-H", fencing_token=3,
            profile_id="load.north_south", idempotency_key="watchdog-cleanup",
            requested_at=NOW,
        )
    )
    assert recovered.succeeded
    assert control_calls[0][control_calls[0].index("--profile") + 1] == "load.north_south"


def test_capture_invoker_checkpoints_model_before_store_process_and_no_golden(tmp_path) -> None:
    model = tmp_path / "model.json"
    model.write_text('{"version": 1}\n', encoding="utf-8")
    script = tmp_path / "capture-eval-case.sh"
    script.write_text("trusted", encoding="utf-8")
    runs = tmp_path / "runs"
    (runs / "run-001").mkdir(parents=True)
    process_calls = []

    def process(argv, **kwargs):
        process_calls.append((argv, kwargs))
        checkpoint = Path(kwargs["env"]["MODEL_SOURCE"])
        assert checkpoint.is_file()
        assert (checkpoint.parent / "model-checkpoint.json").is_file()
        return subprocess.CompletedProcess(argv, 0, "", "")

    invoker = ProductionCaptureInvoker(
        runs_root=runs,
        capture_script=script,
        output_root=tmp_path / "cases",
        model_source=model,
        process_runner=process,
    )
    scheduler = CaptureScheduler(tmp_path / "capture-state.json", clock=Clock(), invoker=invoker)
    scheduler.schedule(
        CaptureRequest(
            run_id="run-001",
            case_id="case-f07-h",
            scenario_id="F07-H",
            scenario_metadata=scenario_metadata(),
            mode="calibration",
            t1="2026-07-16T08:00:00Z",
            t2="2026-07-16T10:20:00Z",
        )
    )
    completed = scheduler.tick("run-001")

    assert completed.capture_start == "2026-07-16T07:50:00Z"
    assert completed.capture_end == "2026-07-16T10:40:00Z"
    assert completed.golden_anomaly_file is False
    assert completed.status == "completed"
    assert len(process_calls) == 1
    assert not (tmp_path / "cases" / "case-f07-h" / "golden.anomaly.json").exists()


def test_capture_invoker_forwards_preflight_and_normal_segment(tmp_path) -> None:
    model = tmp_path / "model.json"
    model.write_text('{"version": 1}\n', encoding="utf-8")
    script = tmp_path / "capture-eval-case.sh"
    script.write_text("trusted", encoding="utf-8")
    runs = tmp_path / "runs"
    run_dir = runs / "run-002"
    run_dir.mkdir(parents=True)
    # The queue drops these next to the run before capture (spec §2.1).
    (run_dir / "preflight.json").write_text('{"verdict": "clean"}\n', encoding="utf-8")
    normal_dir = tmp_path / "normal-segments" / "commerce" / "2026-07-16"
    normal_dir.mkdir(parents=True)
    (run_dir / "normal-segment.path").write_text(str(normal_dir) + "\n", encoding="utf-8")
    process_calls = []

    def process(argv, **kwargs):
        process_calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    invoker = ProductionCaptureInvoker(
        runs_root=runs,
        capture_script=script,
        output_root=tmp_path / "cases",
        model_source=model,
        process_runner=process,
    )
    scheduler = CaptureScheduler(tmp_path / "capture-state.json", clock=Clock(), invoker=invoker)
    scheduler.schedule(
        CaptureRequest(
            run_id="run-002",
            case_id="case-f07-h",
            scenario_id="F07-H",
            scenario_metadata=scenario_metadata(),
            mode="calibration",
            t1="2026-07-16T08:00:00Z",
            t2="2026-07-16T10:20:00Z",
        )
    )
    scheduler.tick("run-002")

    argv = process_calls[0]
    assert argv[argv.index("--preflight-json") + 1] == str(run_dir / "preflight.json")
    assert argv[argv.index("--normal-segment") + 1] == str(normal_dir)


def test_capture_invoker_can_stream_model_from_fixed_remote_observer(tmp_path) -> None:
    runs = tmp_path / "runs"
    (runs / "run-remote").mkdir(parents=True)
    calls = []

    def process(argv, **kwargs):
        calls.append(argv)
        assert argv[:3] == ["ssh", "-i", "/root/.ssh/tb_key"]
        assert argv[-5:] == [
            "docker", "exec", "lucida-ai-observer", "cat",
            "/var/lib/lucida/ai-models/stream-anomaly/global/v1/model.json",
        ]
        return subprocess.CompletedProcess(argv, 0, '{"version": 2}\n', "")

    invoker = ProductionCaptureInvoker(
        runs_root=runs,
        model_source=tmp_path / "missing-model.json",
        model_ssh_target="root@192.168.230.104",
        process_runner=process,
    )
    job = SimpleNamespace(run_id="run-remote")

    invoker.snapshot_model(job, idempotency_key="remote-model")

    assert json.loads((runs / "run-remote" / "model.json").read_text())["version"] == 2
    assert len(calls) == 1


async def test_capture_worker_isolates_one_failed_job_and_completes_another(tmp_path) -> None:
    jobs = {
        "failed": SimpleNamespace(run_id="failed", status="pending"),
        "healthy": SimpleNamespace(run_id="healthy", status="pending"),
    }

    class Scheduler:
        def snapshot(self):
            return SimpleNamespace(jobs=jobs)

        def tick(self, run_id):
            jobs[run_id].status = "failed" if run_id == "failed" else "completed"
            if run_id == "failed":
                raise RuntimeError("isolated failure")
            return jobs[run_id]

    async def no_wait(_seconds):
        return None

    runner = ScenarioRunner(
        tmp_path,
        tmp_path / "logs",
        capture_scheduler=Scheduler(),  # type: ignore[arg-type]
        artifact_store=RunArtifactStore(tmp_path / "runs"),
        sleeper=no_wait,
    )
    await runner._capture_loop()

    assert jobs["failed"].status == "failed"
    assert jobs["healthy"].status == "completed"
    assert "isolated failure" in "\n".join(runner._log_buffer)


async def test_aborted_evaluation_is_not_published_as_an_eval_case(tmp_path) -> None:
    class Invoker:
        def snapshot_model(self, job, *, idempotency_key):
            return None

        def dump_stores(self, job, *, idempotency_key):
            return None

    dispatcher = tmp_path / "run-scenario.sh"
    catalog = tmp_path / "catalog.json"
    dispatcher.write_text("trusted", encoding="utf-8")
    catalog.write_text("{}", encoding="utf-8")
    metadata_path = tmp_path / "scenario-metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "scenarios": {"F07-E": scenario_metadata().model_dump(mode="json")},
            }
        ),
        encoding="utf-8",
    )
    scheduler = CaptureScheduler(
        tmp_path / "capture.json", clock=Clock(), invoker=Invoker()
    )
    store = RunArtifactStore(tmp_path / "runs")
    run_dir = store.create("run-aborted", {"controller": {}})
    runner = ScenarioRunner(
        tmp_path,
        tmp_path / "logs",
        artifact_store=store,
        capture_scheduler=scheduler,
        dispatcher_path=dispatcher,
        catalog_path=catalog,
        scenario_metadata_path=metadata_path,
    )
    session = SimpleNamespace(
        run_id="run-aborted",
        scenario_id="F07-E",
        profile_id="load.north_south",
        approved_profile_id="load.north_south",
        t1=datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc),
        t2=datetime(2026, 7, 16, 10, 20, tzinfo=timezone.utc),
        controller_state=SimpleNamespace(phase=ControllerPhase.ABORTED),
        spec=SimpleNamespace(
            capture=SimpleNamespace(enabled=True),
            adaptive=SimpleNamespace(mode=SimpleNamespace(value="evaluation")),
        ),
        trusted_evidence=lambda: {
            "mode": "evaluation",
            "outcome": "aborted",
            "dirty": False,
            "t1": "2026-07-16T10:00:00Z",
            "t2": "2026-07-16T10:20:00Z",
            "profile": {"kind": "fixed", "id": "load.north_south"},
            "approved_profile_id": "load.north_south",
            "cleanup": {"status": "succeeded"},
            "recovery": {"status": "succeeded"},
        },
    )
    runner._schedule_capture(session, run_dir)
    await runner.stop_capture_worker()

    assert scheduler.snapshot().jobs == {}
    result = json.loads((run_dir / "result.json").read_text())
    assert result["outcome"] == "aborted"
    assert result["mode"] == "evaluation"
    assert "case_id" not in result


async def test_capture_off_skips_the_export_but_still_publishes_the_result(
    tmp_path, monkeypatch
) -> None:
    """Skipping the export must not skip the run's own result.

    result.json is what the queue reads as evidence that the run finished; a
    succeeded run without it stops the batch on "controller evidence missing".
    No case_id either — nothing was published, so claiming one would point at a
    directory that does not exist.
    """
    monkeypatch.setenv("SCENARIO_PASS", "smoke")

    class Invoker:
        def snapshot_model(self, job, *, idempotency_key):
            return None

        def dump_stores(self, job, *, idempotency_key):
            return None

    dispatcher = tmp_path / "run-scenario.sh"
    catalog = tmp_path / "catalog.json"
    dispatcher.write_text("trusted", encoding="utf-8")
    catalog.write_text("{}", encoding="utf-8")
    metadata_path = tmp_path / "scenario-metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "scenarios": {"F07-E": scenario_metadata().model_dump(mode="json")},
            }
        ),
        encoding="utf-8",
    )
    scheduler = CaptureScheduler(
        tmp_path / "capture.json", clock=Clock(), invoker=Invoker()
    )
    store = RunArtifactStore(tmp_path / "runs")
    run_dir = store.create("run-smoke", {"controller": {}})
    runner = ScenarioRunner(
        tmp_path,
        tmp_path / "logs",
        artifact_store=store,
        capture_scheduler=scheduler,
        dispatcher_path=dispatcher,
        catalog_path=catalog,
        scenario_metadata_path=metadata_path,
    )
    session = SimpleNamespace(
        run_id="run-smoke",
        scenario_id="F07-E",
        profile_id="load.north_south",
        approved_profile_id="load.north_south",
        t1=datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc),
        t2=datetime(2026, 7, 16, 10, 20, tzinfo=timezone.utc),
        controller_state=SimpleNamespace(phase=ControllerPhase.SUCCEEDED),
        spec=SimpleNamespace(
            capture=SimpleNamespace(enabled=True),
            adaptive=SimpleNamespace(mode=SimpleNamespace(value="evaluation")),
        ),
        trusted_evidence=lambda: {
            "mode": "evaluation",
            "outcome": "succeeded",
            "dirty": False,
            "t1": "2026-07-16T10:00:00Z",
            "t2": "2026-07-16T10:20:00Z",
            "profile": {"kind": "fixed", "id": "load.north_south"},
            "approved_profile_id": "load.north_south",
            "cleanup": {"status": "succeeded"},
            "recovery": {"status": "succeeded"},
        },
    )
    runner._schedule_capture(session, run_dir)
    await runner.stop_capture_worker()

    assert scheduler.snapshot().jobs == {}
    result = json.loads((run_dir / "result.json").read_text())
    assert result["outcome"] == "succeeded"
    assert "case_id" not in result


def test_append_tick_writes_jsonl_inside_trusted_root(tmp_path) -> None:
    store = RunArtifactStore(tmp_path / "runs")
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)

    store.append_tick(run_dir, {"at": "t0", "phase": "evaluating"})
    store.append_tick(run_dir, {"at": "t1", "phase": "succeeded"})

    lines = (run_dir / "ticks.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [
        {"at": "t0", "phase": "evaluating"},
        {"at": "t1", "phase": "succeeded"},
    ]

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(RuntimeError):
        store.append_tick(outside, {"at": "t2"})


def _dirty_run_capsule(tmp_path, *, executor_body: str):
    """A published capsule whose executor carries `executor_body`."""
    plan = {
        "scenario": {"id": "F21-P", "slug": "adaptive"},
        "live_allowed": True,
        "plan_digest": "b" * 64,
        "profile_instances": [],
    }
    contract = _capsule_contract(tmp_path, plan)
    (contract / "profiles" / "load.py").write_text(executor_body, encoding="utf-8")
    (contract / "profiles" / "load.py").chmod(0o755)
    store = RunArtifactStore(tmp_path / "runs")
    run_dir, _ = store.prepare_capsule(
        "F21-P-run-dirty",
        contract_root=contract,
        scenario_slug="adaptive",
        binding={
            "catalog_slug": "adaptive", "primary_profile": "load.north_south",
            "logical_profile_id": "load.north_south", "companion_profiles": [],
        },
    )
    return store, run_dir, contract


def test_capsule_repair_swaps_the_mechanism_and_leaves_the_target_alone(tmp_path) -> None:
    # The defect that motivated this: a run went DIRTY because its executor was
    # broken, and the same broken executor was frozen inside its capsule, so its
    # own cleanup could never run. DIRTY is global, so that wedged everything.
    store, run_dir, contract = _dirty_run_capsule(tmp_path, executor_body="#!/bin/sh\nbroken\n")
    frozen = run_dir / "capsule" / "contracts" / "profiles" / "load.py"
    assert frozen.read_text() == "#!/bin/sh\nbroken\n"
    plan_before = (run_dir / "plan.json").read_bytes()
    manifest_before = json.loads((run_dir / "capsule.json").read_text())["hash_manifest_sha256"]

    (contract / "profiles" / "load.py").write_text("#!/bin/sh\nfixed\n", encoding="utf-8")
    repair = store.repair_capsule_contracts(
        run_dir, contract,
        reason="executor defect", now=datetime(2026, 7, 30, 8, 30, tzinfo=timezone.utc),
    )

    assert frozen.read_text() == "#!/bin/sh\nfixed\n"
    assert (run_dir / "plan.json").read_bytes() == plan_before, "repair moved the cleanup target"
    assert store.verify_capsule(run_dir)["scenario_slug"] == "adaptive"
    assert repair["previous_hash_manifest_sha256"] == manifest_before
    assert repair["hash_manifest_sha256"] != manifest_before
    recorded = json.loads((run_dir / "capsule-repair.json").read_text())["repairs"]
    assert len(recorded) == 1 and recorded[0]["reason"] == "executor defect"


def test_capsule_repair_refuses_a_run_whose_plan_was_altered(tmp_path) -> None:
    # Repair is allowed to change *how* we undo the injection, never *what* we
    # undo. A run whose plan no longer matches its capsule is a different run
    # wearing this run's id, and re-arming it with working code would be worse
    # than leaving it stuck.
    store, run_dir, contract = _dirty_run_capsule(tmp_path, executor_body="#!/bin/sh\nbroken\n")
    plan = json.loads((run_dir / "plan.json").read_text())
    plan["scenario"]["id"] = "F21-Q"
    (run_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(RuntimeError, match="plan was modified"):
        store.repair_capsule_contracts(
            run_dir, contract, reason="tampered", now=datetime(2026, 7, 30, tzinfo=timezone.utc),
        )


def test_capsule_repair_is_recorded_cumulatively(tmp_path) -> None:
    store, run_dir, contract = _dirty_run_capsule(tmp_path, executor_body="#!/bin/sh\nv1\n")
    for index, body in enumerate(("#!/bin/sh\nv2\n", "#!/bin/sh\nv3\n"), start=1):
        (contract / "profiles" / "load.py").write_text(body, encoding="utf-8")
        store.repair_capsule_contracts(
            run_dir, contract, reason=f"attempt {index}",
            now=datetime(2026, 7, 30, 8, index, tzinfo=timezone.utc),
        )
    recorded = json.loads((run_dir / "capsule-repair.json").read_text())["repairs"]
    assert [item["reason"] for item in recorded] == ["attempt 1", "attempt 2"]
    # Each repair must chain onto the manifest the previous one left behind.
    assert recorded[1]["previous_hash_manifest_sha256"] == recorded[0]["hash_manifest_sha256"]
    assert store.verify_capsule(run_dir)


def test_refused_capsule_repair_leaves_the_capsule_exactly_as_it_was(tmp_path) -> None:
    # A repair that is going to be refused must not have already swapped the
    # contracts. Checking the plan only at the end still raises, but by then the
    # tree and capsule.json have been rewritten and the run is half-repaired:
    # frozen code that no longer matches the manifest anyone audited. The refusal
    # has to happen before the first write.
    store, run_dir, contract = _dirty_run_capsule(tmp_path, executor_body="#!/bin/sh\noriginal\n")
    frozen = run_dir / "capsule" / "contracts" / "profiles" / "load.py"
    capsule_before = (run_dir / "capsule.json").read_bytes()
    manifest_before = (run_dir / "capsule" / "hashes.json").read_bytes()

    plan = json.loads((run_dir / "plan.json").read_text())
    plan["scenario"]["id"] = "F21-Q"
    (run_dir / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (contract / "profiles" / "load.py").write_text("#!/bin/sh\nreplacement\n", encoding="utf-8")

    with pytest.raises(RuntimeError):
        store.repair_capsule_contracts(
            run_dir, contract, reason="refused", now=datetime(2026, 7, 30, tzinfo=timezone.utc),
        )

    assert frozen.read_text() == "#!/bin/sh\noriginal\n", "refused repair still swapped the executor"
    assert (run_dir / "capsule.json").read_bytes() == capsule_before
    assert (run_dir / "capsule" / "hashes.json").read_bytes() == manifest_before
    assert not (run_dir / "capsule-repair.json").exists()


def test_failed_cleanup_is_retried_at_the_dispatcher_layer(tmp_path) -> None:
    # There are two caches on the cleanup path and both used to treat a failure
    # as final: this one, and profile-control's on-disk results map. This layer
    # short-circuits *before* the profile-control call, so fixing only the lower
    # one changes nothing — on 2026-07-30 a repaired capsule with a fixed
    # executor still got the original failure handed back, twice, because the
    # attempt never left this method.
    plan = {
        "scenario": {"id": "F21-P", "slug": "adaptive"},
        "live_allowed": True,
        "plan_digest": "c" * 64,
        "profile_instances": [{"profile_id": "load.north_south", "parameters": {}}],
    }
    contract = _capsule_contract(tmp_path, plan)
    control = contract / "profile-control.py"

    outcomes = [
        {"succeeded": False, "effect_ended_at": None, "reason": "GATEWAY_URL: command not found"},
        {"succeeded": True, "effect_ended_at": "2026-07-30T09:00:00+00:00", "reason": None},
    ]
    calls: list[list[str]] = []

    def process_runner(argv, **kwargs):
        argv = list(argv)
        if argv[0] == str(control):
            calls.append(argv)
            payload = outcomes[min(len(calls) - 1, len(outcomes) - 1)]
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"normalized_plan": plan}), ""
        )

    applier = TrustedDispatcherApplier(
        "adaptive",
        primary_profile="load.north_south",
        logical_profile_id="load.north_south",
        companion_profiles=[],
        dispatcher=contract / "run-scenario.sh",
        profile_control=control,
        process_runner=process_runner,
    )

    def request():
        return CleanupRequest(
            run_id="F21-P-run-dirty", scenario_id="F21-P", fencing_token=124,
            profile_id="load.north_south",
            idempotency_key="manual-cleanup:F21-P-run-dirty:124",
            requested_at=datetime(2026, 7, 30, 9, tzinfo=timezone.utc),
        )

    first = applier.cleanup(request())
    assert first.succeeded is False and len(calls) == 1

    second = applier.cleanup(request())
    assert len(calls) == 2, "the retry never reached profile-control"
    assert second.succeeded is True

    third = applier.cleanup(request())
    assert len(calls) == 2, "a settled cleanup was re-invoked"
    assert third.succeeded is True


def _rewrite_dispatcher_plan(contract: Path, plan: dict) -> None:
    dispatcher = contract / "run-scenario.sh"
    dispatcher.write_text(
        "#!/bin/sh\nprintf '%s\\n' '" + json.dumps({"normalized_plan": plan}) + "'\n",
        encoding="utf-8",
    )
    dispatcher.chmod(0o755)


def test_capsule_repair_recompiles_the_plan_so_cleanup_can_still_load_it(tmp_path) -> None:
    # Freezing plan.json across a repair looks safe and is not: the plan is
    # compiled *from* the contracts, so a stale copy stops matching a recompile
    # and _plan() raises "capsule compiled plan differs from its normalized plan"
    # before cleanup starts. Measured on the real F21-P capsule: the recompile
    # differed in exactly two fields, executor_sha256 and the plan_digest derived
    # from it, with every target field byte-identical.
    store, run_dir, contract = _dirty_run_capsule(tmp_path, executor_body="#!/bin/sh\nbroken\n")
    stored = json.loads((run_dir / "plan.json").read_text())

    moved = json.loads(json.dumps(stored))
    moved["plan_digest"] = "d" * 64
    _rewrite_dispatcher_plan(contract, moved)
    (contract / "profiles" / "load.py").write_text("#!/bin/sh\nfixed\n", encoding="utf-8")

    store.repair_capsule_contracts(
        run_dir, contract, reason="executor defect",
        now=datetime(2026, 7, 30, 9, tzinfo=timezone.utc),
    )
    repaired = json.loads((run_dir / "plan.json").read_text())
    assert repaired["plan_digest"] == "d" * 64, "plan.json was left at the pre-repair digest"
    assert json.loads((run_dir / "capsule.json").read_text())["plan_sha256"] == file_sha256(
        run_dir / "plan.json"
    ), "capsule.json still points at the old plan hash"
    store.verify_capsule(run_dir)


def test_capsule_repair_refuses_when_a_target_field_moves(tmp_path) -> None:
    # executor_sha256 and plan_digest are the mechanism and may move. Anything
    # else — scenario, parameters, approved levels — is the target, and a repair
    # that changes it would clean up something other than what was injected.
    store, run_dir, contract = _dirty_run_capsule(tmp_path, executor_body="#!/bin/sh\nbroken\n")
    stored = json.loads((run_dir / "plan.json").read_text())

    moved = json.loads(json.dumps(stored))
    moved["scenario"]["id"] = "F21-Q"
    _rewrite_dispatcher_plan(contract, moved)

    with pytest.raises(RuntimeError, match="change the cleanup target"):
        store.repair_capsule_contracts(
            run_dir, contract, reason="target moved",
            now=datetime(2026, 7, 30, 9, tzinfo=timezone.utc),
        )
    assert json.loads((run_dir / "plan.json").read_text()) == stored
    assert not (run_dir / "capsule-repair.json").exists()


def _orphaned_dirty_run(root: Path, *, run_id: str, scenario_id: str, token: int, **overrides):
    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "lease.json").write_text(json.dumps({"run_id": run_id, "fencing_token": token}))
    state = {
        "run_id": run_id,
        "scenario_id": scenario_id,
        "status": "dirty",
        "terminal_at": "2026-08-03T10:56:28.363300Z",
        "level_changes": [{"applied_at": "2026-08-03T10:52:10.181024Z", "effect_ended_at": None}],
    }
    state.update(overrides.pop("state", {}))
    (run_dir / "state.json").write_text(json.dumps(state))
    cleanup = overrides.pop("cleanup", {"schema_version": 1, "cleanup": {"succeeded": False}})
    (run_dir / "cleanup.json").write_text(json.dumps(cleanup))
    return run_dir


def test_orphaned_dirty_run_is_findable_once_the_coordinator_forgets_it(tmp_path):
    # 정리를 거부당한 런은 effect_ended_at 없는 level change를 남기고, 그러면 그 구간이
    # 영원히 열려 있어 이후 모든 런의 clean window와 겹친다. 빠져나오는 길은 외부
    # 정리뿐인데, 코디네이터가 그 런을 잊으면 그 길마저 막힌다 — 2026-08-03의
    # F15-T2-run-d5b1ae8a가 그 상태로 모든 라이브 시작을 막았다.
    store = RunArtifactStore(tmp_path)
    _orphaned_dirty_run(tmp_path, run_id="F15-T2-run-open", scenario_id="F15-T2", token=195)
    assert store.find_orphaned_dirty_run("F15-T2") == ("F15-T2-run-open", 195)
    assert store.find_orphaned_dirty_run("F12-H") is None


def test_externally_cleaned_and_still_running_runs_are_not_orphans(tmp_path):
    # 구간을 닫는 것은 외부 정리가 쓰는 평면 형태({"succeeded": true, "effect_ended_at"})
    # 뿐이다. persist_session 이 쓰는 중첩 형태는 닫지 않는다 — 이 구분을 잃으면
    # 이미 해소된 런까지 다시 인수해 되돌린다.
    store = RunArtifactStore(tmp_path)
    _orphaned_dirty_run(
        tmp_path, run_id="F16-H-run-closed", scenario_id="F16-H", token=190,
        cleanup={"schema_version": 1, "succeeded": True,
                 "effect_ended_at": "2026-08-03T05:09:08.084997+00:00"},
    )
    assert store.find_orphaned_dirty_run("F16-H") is None

    _orphaned_dirty_run(
        tmp_path, run_id="F16-H-run-live", scenario_id="F16-H", token=191,
        state={"status": "running"},
    )
    assert store.find_orphaned_dirty_run("F16-H") is None


def test_readopt_takes_the_run_back_only_from_an_idle_coordinator(tmp_path):
    coordinator = GlobalCoordinator(tmp_path / "coordinator.json")
    now = datetime(2026, 8, 4, 0, tzinfo=timezone.utc)
    dirty = coordinator.readopt_dirty(
        run_id="F15-T2-run-open", scenario_id="F15-T2", fencing_token=195,
        reason="readopted", now=now,
    )
    assert dirty.run_id == "F15-T2-run-open"
    assert coordinator.snapshot().dirty_run is not None

    # 이미 무언가를 들고 있으면 그쪽이 기록이다. 덮어쓰면 살아 있는 런을 잃는다.
    with pytest.raises(RuntimeError, match="another dirty run"):
        coordinator.readopt_dirty(
            run_id="F15-T2-run-other", scenario_id="F15-T2", fencing_token=196,
            reason="readopted", now=now,
        )


@pytest.mark.asyncio
async def test_a_slow_apply_does_not_starve_the_heartbeat(tmp_path) -> None:
    # F15-T2 carries start_offset_seconds=240 while the coordinator lease is 30s.
    # When apply ran on the event loop the heartbeat could not tick for those four
    # minutes, the lease expired, and every later operation was refused with
    # "operation rejected by fencing token" — the scenario was structurally
    # unrunnable. apply is offloaded now; this pins that so the blocking form
    # cannot come back.
    import asyncio
    import time

    from app.production_runtime import AsyncProfileApplier

    class BlockingApplier:
        def apply(self, request):
            time.sleep(0.4)  # stands in for the 4-minute offset wait
            return "applied"

    applier = AsyncProfileApplier(BlockingApplier())
    beats = 0
    beats_when_apply_returned = None

    async def do_apply() -> object:
        nonlocal beats_when_apply_returned
        result = await applier.apply(object())
        # Counting beats at the END would pass either way — the starved
        # heartbeat simply catches up once apply lets go of the loop. The
        # question is how many beats landed *while apply was in flight*.
        beats_when_apply_returned = beats
        return result

    async def heartbeat() -> None:
        nonlocal beats
        for _ in range(20):
            await asyncio.sleep(0.02)
            beats += 1

    result, _ = await asyncio.gather(do_apply(), heartbeat())

    assert result == "applied"
    # ~0.4s of apply against a 0.02s heartbeat: concurrent gives well over 10.
    # Running apply on the event loop gives exactly 0.
    assert beats_when_apply_returned is not None
    assert beats_when_apply_returned >= 10, (
        f"heartbeat was starved during apply (beats={beats_when_apply_returned})"
    )
