"""Persistent, clock-driven orchestration for post-scenario data capture.

The scheduler deliberately has no timer or subprocess implementation.  A caller
ticks it with an injected clock and supplies an idempotent invoker.  This keeps
the timing/state contract testable while leaving production process ownership
to the scenario runner.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
# Capture window contract v2 (spec-eval-data-capture §2.1, 2026-07-20):
# scenario segment is [t1-10m, t2+20m]. The 2h normal lead-in became a shared
# daily normal segment, so the scenario window no longer waits two hours.
PRE_WINDOW = timedelta(minutes=10)
POST_WINDOW = timedelta(minutes=20)
MODEL_PATH = "/var/lib/lucida/ai-models/stream-anomaly/global/v1/model.json"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScenarioMetadata(BaseModel):
    # The registry carries additional design fields (class, root_cause,
    # must_support, ... — scenario-redesign 2026-07-24). Capture's canonical
    # metadata is exactly these six; extras are ignored so registry evolution
    # cannot brick queue readiness, and sha256() stays a six-field digest.
    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    cause: str = Field(min_length=1)
    injection_summary: str = Field(min_length=1)
    user_impact: str = Field(min_length=1)
    distinguishing_evidence: str = Field(min_length=1)

    def sha256(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()


class ScenarioMetadataRegistry(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    scenarios: dict[str, ScenarioMetadata]


def load_scenario_metadata(path: Path, scenario_id: str) -> ScenarioMetadata:
    registry = ScenarioMetadataRegistry.model_validate_json(path.read_text(encoding="utf-8"))
    metadata = registry.scenarios.get(scenario_id)
    if metadata is None:
        raise RuntimeError(f"AI-authored scenario metadata is missing: {scenario_id}")
    return metadata


def parse_utc(value: str, *, field: str) -> datetime:
    """Parse the one timestamp representation accepted by capture artifacts."""
    if not UTC_TIMESTAMP.fullmatch(value):
        raise ValueError(f"{field} must use YYYY-MM-DDTHH:MM:SSZ UTC form")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise ValueError(f"{field} is not a valid UTC timestamp") from error
    if format_utc(parsed) != value:
        raise ValueError(f"{field} is not a normalized UTC timestamp")
    return parsed


def format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("clock values must be timezone-aware")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class CaptureEvidence(StrictModel):
    """Inputs needed to produce controller-owned evaluation ``result.json``."""

    approved_profile_id: str = Field(min_length=1)
    profile_id: str = Field(min_length=1)
    profile_kind: Literal["fixed"] = "fixed"
    cleanup_status: Literal["succeeded"] = "succeeded"
    recovery_status: Literal["succeeded"] = "succeeded"
    dirty: Literal[False] = False
    plan_sha256: str = Field(pattern=SHA256.pattern)
    script_sha256: str = Field(pattern=SHA256.pattern)
    catalog_sha256: str = Field(pattern=SHA256.pattern)
    script_path: str = Field(min_length=1)
    catalog_path: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_approved_profile(self) -> "CaptureEvidence":
        if self.profile_id != self.approved_profile_id:
            raise ValueError("evaluation profile must match the approved profile")
        if not Path(self.script_path).is_absolute() or not Path(self.catalog_path).is_absolute():
            raise ValueError("evaluation artifact paths must be absolute")
        return self


class CaptureRequest(StrictModel):
    run_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    case_id: str = Field(pattern=r"^case-[a-z0-9][a-z0-9-]*$")
    scenario_id: str = Field(min_length=1)
    scenario_metadata: ScenarioMetadata | None = None
    mode: Literal["calibration", "evaluation", "failed"]
    t1: str
    t2: str
    evidence: CaptureEvidence | None = None

    @model_validator(mode="after")
    def validate_request(self) -> "CaptureRequest":
        t1 = parse_utc(self.t1, field="t1")
        t2 = parse_utc(self.t2, field="t2")
        if t2 < t1:
            raise ValueError("t2 must be greater than or equal to t1")
        if self.mode == "evaluation" and self.evidence is None:
            raise ValueError("evaluation capture requires fixed-profile evidence")
        if self.mode in {"calibration", "failed"} and self.evidence is not None:
            raise ValueError(f"{self.mode} capture must not claim evaluation evidence")
        return self


CaptureStatus = Literal["pending", "model_requested", "completed", "failed"]
MAX_CAPTURE_ATTEMPTS = 3


class CaptureJob(CaptureRequest):
    schema_version: Literal[1] = 1
    status: CaptureStatus = "pending"
    capture_start: str
    capture_end: str
    model_snapshot_path: Literal[MODEL_PATH] = MODEL_PATH
    golden_anomaly_file: Literal[False] = False
    scheduled_at: str
    model_snapshot_requested_at: str | None = None
    model_snapshot_lag_sec: int | None = Field(default=None, ge=0)
    completed_at: str | None = None
    failure: str | None = None
    attempt_count: int = Field(default=0, ge=0, le=MAX_CAPTURE_ATTEMPTS)
    next_retry_at: str | None = None

    @model_validator(mode="after")
    def validate_derived_window(self) -> "CaptureJob":
        t1 = parse_utc(self.t1, field="t1")
        t2 = parse_utc(self.t2, field="t2")
        if self.capture_start != format_utc(t1 - PRE_WINDOW):
            raise ValueError("capture_start must equal t1-10m")
        if self.capture_end != format_utc(t2 + POST_WINDOW):
            raise ValueError("capture_end must equal t2+20m")
        parse_utc(self.scheduled_at, field="scheduled_at")
        if self.model_snapshot_requested_at is not None:
            requested = parse_utc(
                self.model_snapshot_requested_at, field="model_snapshot_requested_at"
            )
            end = parse_utc(self.capture_end, field="capture_end")
            if requested < end:
                raise ValueError("model snapshot cannot be requested before capture_end")
            expected_lag = int((requested - end).total_seconds())
            if self.model_snapshot_lag_sec != expected_lag:
                raise ValueError("model snapshot lag does not match its request time")
        if self.completed_at is not None:
            parse_utc(self.completed_at, field="completed_at")
        if self.next_retry_at is not None:
            parse_utc(self.next_retry_at, field="next_retry_at")
        return self

    def trusted_result(self) -> dict[str, object]:
        """Return the exact evaluation metadata consumed by capture-eval-case.sh."""
        if self.mode != "evaluation" or self.evidence is None:
            raise RuntimeError("only evaluation jobs have trusted result metadata")
        evidence = self.evidence
        return {
            "mode": "evaluation",
            "outcome": "succeeded",
            "dirty": evidence.dirty,
            "case_id": self.case_id,
            "scenario_id": self.scenario_id,
            "scenario_metadata": self.scenario_metadata.model_dump(mode="json")
            if self.scenario_metadata is not None
            else None,
            "scenario_metadata_sha256": self.scenario_metadata.sha256()
            if self.scenario_metadata is not None
            else None,
            "t1": self.t1,
            "t2": self.t2,
            "profile": {"kind": evidence.profile_kind, "id": evidence.profile_id},
            "approved_profile_id": evidence.approved_profile_id,
            "cleanup": {"status": evidence.cleanup_status},
            "recovery": {"status": evidence.recovery_status},
            "plan_sha256": evidence.plan_sha256,
            "script_sha256": evidence.script_sha256,
            "catalog_sha256": evidence.catalog_sha256,
            "script_path": evidence.script_path,
            "catalog_path": evidence.catalog_path,
        }


class CaptureState(StrictModel):
    schema_version: Literal[1] = 1
    jobs: dict[str, CaptureJob] = Field(default_factory=dict)


class Clock(Protocol):
    def now(self) -> datetime: ...


class CaptureInvoker(Protocol):
    """Production implementations must honor each stable idempotency key."""

    def snapshot_model(self, job: CaptureJob, *, idempotency_key: str) -> None: ...

    def dump_stores(self, job: CaptureJob, *, idempotency_key: str) -> None: ...


class CaptureScheduler:
    """Persistent state machine advanced explicitly by ``tick`` calls."""

    def __init__(self, state_path: Path, *, clock: Clock, invoker: CaptureInvoker) -> None:
        self.state_path = state_path
        self.lock_path = state_path.with_suffix(state_path.suffix + ".lock")
        self.clock = clock
        self.invoker = invoker

    def snapshot(self) -> CaptureState:
        if not self.state_path.exists():
            return CaptureState()
        return CaptureState.model_validate_json(self.state_path.read_text(encoding="utf-8"))

    def schedule(self, request: CaptureRequest) -> CaptureJob:
        with self._locked() as state:
            existing = state.jobs.get(request.run_id)
            if existing is not None:
                existing_request = {
                    name: getattr(existing, name) for name in CaptureRequest.model_fields
                }
                if request.model_dump(mode="json") != CaptureRequest(
                    **existing_request
                ).model_dump(mode="json"):
                    raise RuntimeError(f"run_id already has a different capture job: {request.run_id}")
                return existing
            now = self._now()
            t1 = parse_utc(request.t1, field="t1")
            t2 = parse_utc(request.t2, field="t2")
            job = CaptureJob(
                **request.model_dump(mode="json"),
                capture_start=format_utc(t1 - PRE_WINDOW),
                capture_end=format_utc(t2 + POST_WINDOW),
                scheduled_at=format_utc(now),
            )
            self._write(state.model_copy(update={"jobs": {**state.jobs, request.run_id: job}}))
            return job

    def tick(self, run_id: str) -> CaptureJob:
        """Advance one job without sleeping; completed calls are idempotent."""
        with self._locked() as state:
            job = state.jobs.get(run_id)
            if job is None:
                raise KeyError(f"unknown capture job: {run_id}")
            if job.status in {"completed", "failed"}:
                return job
            now = self._now()
            due = parse_utc(job.capture_end, field="capture_end")
            if now < due:
                return job
            if job.next_retry_at is not None and now < parse_utc(
                job.next_retry_at, field="next_retry_at"
            ):
                return job
            try:
                if job.status == "pending":
                    self.invoker.snapshot_model(
                        job, idempotency_key=f"capture:{run_id}:model"
                    )
                    requested_at = format_utc(now)
                    job = job.model_copy(
                        update={
                            "status": "model_requested",
                            "model_snapshot_requested_at": requested_at,
                            "model_snapshot_lag_sec": int((now - due).total_seconds()),
                        }
                    )
                    state = self._replace_job(state, job)
                    self._write(state)

                # The model checkpoint is persisted before any slower store dump.
                self.invoker.dump_stores(job, idempotency_key=f"capture:{run_id}:stores")
                job = job.model_copy(
                    update={
                        "status": "completed",
                        "completed_at": format_utc(self._now()),
                        "failure": None,
                        "next_retry_at": None,
                    }
                )
                self._write(self._replace_job(state, job))
                return job
            except Exception as error:
                attempts = job.attempt_count + 1
                terminal = attempts >= MAX_CAPTURE_ATTEMPTS
                retry_at = None if terminal else format_utc(
                    self._now() + timedelta(seconds=30 * (2 ** (attempts - 1)))
                )
                failed = job.model_copy(
                    update={
                        "status": "failed" if terminal else job.status,
                        "failure": str(error),
                        "attempt_count": attempts,
                        "next_retry_at": retry_at,
                    }
                )
                self._write(self._replace_job(state, failed))
                return failed

    def _now(self) -> datetime:
        value = self.clock.now()
        if value.tzinfo is None:
            raise ValueError("clock values must be timezone-aware")
        return value.astimezone(timezone.utc).replace(microsecond=0)

    @contextmanager
    def _locked(self) -> Iterator[CaptureState]:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield self.snapshot()

    def _replace_job(self, state: CaptureState, job: CaptureJob) -> CaptureState:
        return state.model_copy(update={"jobs": {**state.jobs, job.run_id: job}})

    def _write(self, state: CaptureState) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(prefix=f".{self.state_path.name}.", dir=self.state_path.parent)
        temp_path = Path(raw_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(state.model_dump_json(indent=2))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.state_path)
        finally:
            temp_path.unlink(missing_ok=True)
