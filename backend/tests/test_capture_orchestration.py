from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.capture_orchestration import (
    CaptureEvidence,
    CaptureRequest,
    CaptureScheduler,
    ScenarioMetadata,
)


class FakeClock:
    def __init__(self, value: str) -> None:
        self.value = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )

    def now(self) -> datetime:
        return self.value

    def set(self, value: str) -> None:
        self.value = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )


class FakeInvoker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def snapshot_model(self, job, *, idempotency_key: str) -> None:
        self.calls.append(("model", job.run_id, idempotency_key))

    def dump_stores(self, job, *, idempotency_key: str) -> None:
        self.calls.append(("stores", job.run_id, idempotency_key))


class SimulatedProcessExit(BaseException):
    pass


class CrashOnceInvoker(FakeInvoker):
    def __init__(self) -> None:
        super().__init__()
        self.crash = True

    def dump_stores(self, job, *, idempotency_key: str) -> None:
        if self.crash:
            self.crash = False
            raise SimulatedProcessExit
        super().dump_stores(job, idempotency_key=idempotency_key)


def calibration_request() -> CaptureRequest:
    return CaptureRequest(
        run_id="run-001",
        case_id="case-f07-h",
        scenario_id="F07-H",
        mode="calibration",
        t1="2026-07-16T10:00:00Z",
        t2="2026-07-16T10:20:00Z",
    )


def evidence() -> CaptureEvidence:
    digest = "a" * 64
    return CaptureEvidence(
        approved_profile_id="load-north-south-80rps",
        profile_id="load-north-south-80rps",
        plan_sha256=digest,
        script_sha256="b" * 64,
        catalog_sha256="c" * 64,
        script_path="/opt/scenarios/f07-h.sh",
        catalog_path="/opt/scenarios/catalog.json",
    )


def scenario_metadata() -> ScenarioMetadata:
    return ScenarioMetadata(
        title="North-south surge",
        description="A bounded user traffic surge exercises the checkout path.",
        cause="North-south traffic surge",
        injection_summary="Inject approved k6 traffic through the public NodePort.",
        user_impact="Checkout latency and errors increase.",
        distinguishing_evidence="Ingress and internal call volume rise proportionally.",
    )


def test_strict_utc_and_mode_evidence_contract() -> None:
    with pytest.raises(ValidationError, match="YYYY-MM-DDTHH:MM:SSZ"):
        CaptureRequest(
            **{
                **calibration_request().model_dump(),
                "t1": "2026-07-16T10:00:00+00:00",
            }
        )

    with pytest.raises(ValidationError, match="requires fixed-profile evidence"):
        CaptureRequest(
            run_id="r1",
            case_id="case-one",
            scenario_id="F01-A",
            mode="evaluation",
            t1="2026-07-16T10:00:00Z",
            t2="2026-07-16T10:10:00Z",
        )

    with pytest.raises(ValidationError, match="approved profile"):
        CaptureEvidence(**{**evidence().model_dump(), "profile_id": "unapproved"})


def test_window_wait_model_first_persistence_and_duplicate_ticks(tmp_path) -> None:
    clock = FakeClock("2026-07-16T09:00:00Z")
    invoker = FakeInvoker()
    state_path = tmp_path / "capture-jobs.json"
    scheduler = CaptureScheduler(state_path, clock=clock, invoker=invoker)

    job = scheduler.schedule(calibration_request())
    assert job.capture_start == "2026-07-16T09:50:00Z"
    assert job.capture_end == "2026-07-16T10:40:00Z"
    assert job.golden_anomaly_file is False
    assert scheduler.schedule(calibration_request()) == job

    clock.set("2026-07-16T10:39:59Z")
    assert scheduler.tick(job.run_id).status == "pending"
    assert invoker.calls == []

    clock.set("2026-07-16T10:40:00Z")
    completed = scheduler.tick(job.run_id)
    assert completed.status == "completed"
    assert completed.model_snapshot_requested_at == "2026-07-16T10:40:00Z"
    assert completed.model_snapshot_lag_sec == 0
    assert [call[0] for call in invoker.calls] == ["model", "stores"]

    restored = CaptureScheduler(state_path, clock=clock, invoker=invoker)
    assert restored.snapshot().jobs[job.run_id].status == "completed"
    assert restored.tick(job.run_id).status == "completed"
    assert len(invoker.calls) == 2
    persisted = json.loads(state_path.read_text())
    assert persisted["jobs"][job.run_id]["golden_anomaly_file"] is False


def test_evaluation_metadata_matches_trusted_capture_result_contract(tmp_path) -> None:
    clock = FakeClock("2026-07-16T12:00:00Z")
    scheduler = CaptureScheduler(tmp_path / "state.json", clock=clock, invoker=FakeInvoker())
    request = CaptureRequest(
        run_id="eval-001",
        case_id="case-f07-h-eval",
        scenario_id="F07-H",
        scenario_metadata=scenario_metadata(),
        mode="evaluation",
        t1="2026-07-16T10:00:00Z",
        t2="2026-07-16T10:20:00Z",
        evidence=evidence(),
    )
    result = scheduler.schedule(request).trusted_result()

    assert result["mode"] == "evaluation"
    assert result["outcome"] == "succeeded"
    assert result["dirty"] is False
    assert result["profile"] == {
        "kind": "fixed",
        "id": "load-north-south-80rps",
    }
    assert result["approved_profile_id"] == result["profile"]["id"]
    assert result["cleanup"] == {"status": "succeeded"}
    assert result["recovery"] == {"status": "succeeded"}
    assert result["t1"] == request.t1 and result["t2"] == request.t2
    assert result["scenario_metadata"] == scenario_metadata().model_dump(mode="json")
    assert result["scenario_metadata_sha256"] == scenario_metadata().sha256()


def test_restore_resumes_store_dump_without_repeating_model_request(tmp_path) -> None:
    clock = FakeClock("2026-07-16T10:40:00Z")
    invoker = CrashOnceInvoker()
    state_path = tmp_path / "state.json"
    scheduler = CaptureScheduler(state_path, clock=clock, invoker=invoker)
    scheduler.schedule(calibration_request())

    with pytest.raises(SimulatedProcessExit):
        scheduler.tick("run-001")
    assert scheduler.snapshot().jobs["run-001"].status == "model_requested"

    restored = CaptureScheduler(state_path, clock=clock, invoker=invoker)
    assert restored.tick("run-001").status == "completed"
    assert [call[0] for call in invoker.calls] == ["model", "stores"]


def test_different_request_cannot_reuse_run_id(tmp_path) -> None:
    scheduler = CaptureScheduler(
        tmp_path / "state.json",
        clock=FakeClock("2026-07-16T09:00:00Z"),
        invoker=FakeInvoker(),
    )
    scheduler.schedule(calibration_request())
    changed = calibration_request().model_copy(update={"case_id": "case-different"})
    with pytest.raises(RuntimeError, match="different capture job"):
        scheduler.schedule(changed)


def test_failed_label_never_accepts_evaluation_evidence(tmp_path) -> None:
    with pytest.raises(ValidationError, match="failed capture must not claim"):
        CaptureRequest(
            run_id="failed-001",
            case_id="case-failed-eval",
            scenario_id="F07-E",
            mode="failed",
            t1="2026-07-16T10:00:00Z",
            t2="2026-07-16T10:20:00Z",
            evidence=evidence(),
        )


def test_failed_capture_attempt_survives_restart_and_retries(tmp_path) -> None:
    class FailingInvoker(FakeInvoker):
        def snapshot_model(self, job, *, idempotency_key: str) -> None:
            raise RuntimeError("model unavailable")

    state = tmp_path / "failed-state.json"
    scheduler = CaptureScheduler(
        state,
        clock=FakeClock("2026-07-16T10:40:00Z"),
        invoker=FailingInvoker(),
    )
    scheduler.schedule(calibration_request())
    first = scheduler.tick("run-001")
    assert first.attempt_count == 1 and first.failure == "model unavailable"

    restored = CaptureScheduler(
        state,
        clock=FakeClock("2026-07-16T12:00:00Z"),
        invoker=FakeInvoker(),
    )
    assert restored.snapshot().jobs["run-001"].attempt_count == 1
    assert restored.tick("run-001").status == "completed"
