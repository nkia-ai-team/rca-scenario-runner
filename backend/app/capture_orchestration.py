"""Persistent, clock-driven orchestration for post-scenario data capture.

The scheduler deliberately has no timer or subprocess implementation.  A caller
ticks it with an injected clock and supplies an idempotent invoker.  This keeps
the timing/state contract testable while leaving production process ownership
to the scenario runner.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
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


# Whether an accepted run is exported at all is a property of the pass, not of
# the capture machinery: see app.pass_mode.capture_enabled.


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


class BeyondDataNote(StrictModel):
    detail: str = Field(min_length=1)
    missing_data: str = Field(min_length=1)
    confirmed_by: str = Field(min_length=1)


class ScenarioNotes(StrictModel):
    """Reference-only notes carried into meta.json as ``scenario_notes``.

    The registry's optional ``beyond_data``/``beyond_data_ko`` hold the detailed
    cause that collected data cannot separate, so it stays out of the six scored
    fields above and out of their digest. Scenarios without notes capture exactly
    as before.
    """

    beyond_data: BeyondDataNote
    beyond_data_ko: BeyondDataNote


class ScenarioMetadataRegistry(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    scenarios: dict[str, ScenarioMetadata]


def load_scenario_metadata(path: Path, scenario_id: str) -> ScenarioMetadata:
    registry = ScenarioMetadataRegistry.model_validate_json(path.read_text(encoding="utf-8"))
    metadata = registry.scenarios.get(scenario_id)
    if metadata is None:
        raise RuntimeError(f"AI-authored scenario metadata is missing: {scenario_id}")
    return metadata


def load_scenario_notes(path: Path, scenario_id: str) -> ScenarioNotes | None:
    registry = json.loads(path.read_text(encoding="utf-8"))
    entry = registry.get("scenarios", {}).get(scenario_id) or {}
    notes = {key: entry[key] for key in ("beyond_data", "beyond_data_ko") if key in entry}
    if not notes:
        return None
    return ScenarioNotes.model_validate(notes)


# --- Recording answer sheet and source commits (2026-11-18 testbed v4 CAPMETA) ---
#
# v3 campaign and v4 recordings had no scenario.md: neither the runner nor the capture
# ever wrote one (the July v3 folders carry one a supervisor session authored after the
# capture), and meta held no commit of the testbed-services copy the run used. The
# runner now freezes both next to the run when it schedules the capture, and the
# capture carries them into the case (scenario.md at the case root, meta.source_commits).

COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SOURCE_COMMIT_MARKER = "SOURCE_COMMIT"
RUNNER_SOURCE_COMMIT_ENV = "RUNNER_SOURCE_COMMIT"
# The runner's own marker, baked into the image (Dockerfile copies backend/app).
RUNNER_SOURCE_COMMIT_MARKER = Path(__file__).resolve().parent / SOURCE_COMMIT_MARKER
SCENARIO_DOC_FILE = "scenario.md"
# The capture flags this runner passes (dump_stores). Readiness asks the capture script the
# container actually sees for them (``--capabilities``), so a runner deployed ahead of its
# capture stops before a cycle instead of losing the recording to "unknown argument".
REQUIRED_CAPTURE_CAPABILITIES = frozenset({"scenario-doc-file", "source-commits-json"})
SOURCE_COMMITS_FILE = "source-commits.json"


def parse_source_commit(raw: str | None, *, source: str) -> dict[str, object]:
    """One deploy marker as ``{commit, status, source}``.

    ``git archive`` writes the archived commit into the marker (export-subst); the
    rsync deploy writes HEAD, with ``-unpushed`` when that commit was on no remote
    branch and ``-dirty`` when the deployed tree had uncommitted changes. A copy that
    went through neither still holds the placeholder. ``deploying <commit>`` (a deploy
    in progress or cut short) and anything else is invalid: no commit is claimed.
    """
    if raw is None:
        return {"commit": None, "status": "missing", "source": source}
    value = raw.strip()
    for suffix, status in (("-unpushed-dirty", "unpushed_dirty"), ("-unpushed", "unpushed"), ("-dirty", "dirty"), ("", "stamped")):
        base = value[: len(value) - len(suffix)] if suffix else value
        if value.endswith(suffix) and COMMIT_SHA.fullmatch(base):
            return {"commit": base, "status": status, "source": source}
    if value.startswith("$Format:"):
        return {"commit": None, "status": "unstamped", "source": source}
    return {"commit": None, "status": "invalid", "source": source}


UNREADABLE_MARKER = "unreadable"


def _read_marker(path: Path) -> str | None:
    """The marker text, None when absent. A marker that cannot be read (a directory, no
    permission, not UTF-8) reads as invalid: the commit record is not a scoring input, so
    it must not cost the recording its capture (review L2)."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return UNREADABLE_MARKER


def read_runner_source_commit(
    marker: Path = RUNNER_SOURCE_COMMIT_MARKER, *, environ: dict[str, str] | None = None
) -> dict[str, object]:
    """The runner's commit: the stamped image marker, else the build-time env value."""
    from_marker = parse_source_commit(_read_marker(marker), source="backend/app/SOURCE_COMMIT")
    if from_marker["commit"] is not None:
        return from_marker
    env_value = (environ if environ is not None else os.environ).get(RUNNER_SOURCE_COMMIT_ENV, "").strip()
    if env_value:
        return parse_source_commit(env_value, source=f"env:{RUNNER_SOURCE_COMMIT_ENV}")
    return from_marker


def collect_source_commits(
    contract_root: Path,
    *,
    runner_marker: Path = RUNNER_SOURCE_COMMIT_MARKER,
    environ: dict[str, str] | None = None,
) -> dict[str, object]:
    """``meta.source_commits``: the testbed-services copy the runner mounts, and the runner."""
    return {
        "schema_version": 1,
        "testbed_services": parse_source_commit(
            _read_marker(contract_root / SOURCE_COMMIT_MARKER),
            source=f"scenario-contracts/{SOURCE_COMMIT_MARKER}",
        ),
        "runner": read_runner_source_commit(runner_marker, environ=environ),
    }


def _commit_cell(entry: dict[str, object]) -> str:
    if entry["commit"] is None:
        return f"(없음, {entry['status']})"
    return f"{entry['commit']} ({entry['status']})"


def _doc_value(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "\n".join(f"- {item}" for item in value) if value else "(빈 목록)"
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```"


def render_scenario_doc(
    registry_path: Path,
    scenario_id: str,
    *,
    case_id: str,
    run_id: str,
    source_commits: dict[str, object],
) -> str:
    """The recording's answer sheet: the registry entry, field by field, in its own words.

    Only the heading and the field titles are the runner's; every value is the
    registry text as written (strings as paragraphs, string lists as bullets, the
    rest as JSON), followed by the entry's raw JSON so a golden can be built from
    the case alone. No sentence is generated from IDs.
    """
    raw = registry_path.read_bytes()
    registry = json.loads(raw.decode("utf-8"))
    entry = registry.get("scenarios", {}).get(scenario_id)
    if not isinstance(entry, dict):
        raise RuntimeError(f"AI-authored scenario metadata is missing: {scenario_id}")
    lines = [
        f"# 시나리오 정답지: {scenario_id}",
        "",
        "이 파일은 녹화 때 러너가 시나리오 등록부(scripts/scenarios/registry/scenario-metadata.json)의 "
        "이 시나리오 항목을 그대로 옮긴 정답지다. 평가 대상 AI 와 판정관에게 주지 않는다. "
        "칸 내용은 등록부 원문이고 러너가 쓴 것은 이 머리말과 칸 제목뿐이다. "
        "등록부 원문은 설계 때 쓴 글이라 그 안의 수치와 녹화 이름은 이 녹화의 실측이 아니다.",
        "",
        "| 항목 | 값 |",
        "|---|---|",
        f"| scenario_id | {scenario_id} |",
        f"| case_id | {case_id} |",
        f"| run_id | {run_id} |",
        f"| 등록부 sha256 | {hashlib.sha256(raw).hexdigest()} |",
        f"| testbed-services 커밋(scripts/scenarios 배포 기준) | {_commit_cell(source_commits['testbed_services'])} |",
        f"| 러너 커밋 | {_commit_cell(source_commits['runner'])} |",
    ]
    for key, value in entry.items():
        lines += ["", f"## {key}", "", _doc_value(value)]
    lines += [
        "",
        "## 등록부 항목 원문 (JSON)",
        "",
        "```json",
        json.dumps(entry, ensure_ascii=False, indent=2),
        "```",
        "",
    ]
    return "\n".join(lines)


def _atomic_write_text(path: Path, text: str) -> None:
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def freeze_capture_inputs(
    run_dir: Path,
    *,
    registry_path: Path,
    contract_root: Path,
    scenario_id: str,
    case_id: str,
    run_id: str,
    runner_marker: Path = RUNNER_SOURCE_COMMIT_MARKER,
    environ: dict[str, str] | None = None,
) -> None:
    """Write source-commits.json and scenario.md next to the run, once.

    They are taken when the capture is scheduled, from the same registry the six
    scored fields came from. A capture retried later (re-arm) carries what the run
    used, not whatever was deployed meanwhile, so existing files are kept.
    """
    commits_path = run_dir / SOURCE_COMMITS_FILE
    if commits_path.exists():
        source_commits = json.loads(commits_path.read_text(encoding="utf-8"))
    else:
        source_commits = collect_source_commits(
            contract_root, runner_marker=runner_marker, environ=environ
        )
        _atomic_write_text(commits_path, json.dumps(source_commits, indent=2, sort_keys=True) + "\n")
    doc_path = run_dir / SCENARIO_DOC_FILE
    if not doc_path.exists():
        _atomic_write_text(
            doc_path,
            render_scenario_doc(
                registry_path, scenario_id, case_id=case_id, run_id=run_id, source_commits=source_commits
            ),
        )


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
    scenario_notes: ScenarioNotes | None = None
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
    # Operator re-arm of a terminally failed job (2026-11-13 CAPFIX): the queue's
    # cycle resume retries only the capture of a run whose injection succeeded.
    # Each re-arm keeps the failure it cleared so the job still tells its story.
    rearm_count: int = Field(default=0, ge=0)
    rearmed_at: str | None = None
    previous_failures: list[str] = Field(default_factory=list)

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
        if self.rearmed_at is not None:
            parse_utc(self.rearmed_at, field="rearmed_at")
        if len(self.previous_failures) != self.rearm_count:
            raise ValueError("every re-arm must keep the failure it cleared")
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

    def rearm(self, run_id: str) -> CaptureJob:
        """Give a terminally failed job a fresh attempt budget for the same run.

        Only the attempt bookkeeping changes. The request, and so the capture
        window (t1, t2, and the run's phases.json the invoker forwards), stays
        exactly what the first attempts used. A job whose model checkpoint was
        already taken resumes at the store dump, like a restart does, so the
        checkpoint keeps the moment right after the window rather than the
        moment of the retry.
        """
        with self._locked() as state:
            job = state.jobs.get(run_id)
            if job is None:
                raise KeyError(f"unknown capture job: {run_id}")
            if job.status != "failed":
                raise RuntimeError(f"only a failed capture job can be re-armed: {run_id} is {job.status}")
            job = job.model_copy(
                update={
                    "status": "pending" if job.model_snapshot_requested_at is None else "model_requested",
                    "attempt_count": 0,
                    "next_retry_at": None,
                    "failure": None,
                    "rearm_count": job.rearm_count + 1,
                    "rearmed_at": format_utc(self._now()),
                    "previous_failures": [*job.previous_failures, job.failure or "unknown failure"],
                }
            )
            job = CaptureJob.model_validate(job.model_dump(mode="json"))
            self._write(self._replace_job(state, job))
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
