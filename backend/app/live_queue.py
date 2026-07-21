"""Persistent, fail-closed orchestration for the approved live scenario sequence."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import stat
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.adaptive import ControllerPhase
from app.adaptive_runtime import ControllerSession, SessionStatus
from app.capture_orchestration import CaptureJob, load_scenario_metadata, parse_utc
from app.preflight import (
    AiJudge,
    PreflightVerdict,
    build_preflight_checks,
    evaluate_preflight,
)
from app.runner import ScenarioRunner, get_runner


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


LIVE_SCENARIO_ORDER = (
    "F01-R",
    "F01-H",
    "F03-G",
    "F06-R",
    "F07-H",
    "F08-H",
    "F09-P",
    "F11-G",
)
QUEUE_POLL_INTERVAL_SEC = 5
# Daily normal-segment protection (spec §2.1): the 00:00-02:00 KST window is
# the shared clean prefix for every case captured that day, and the host cron
# dumps it at 02:20 KST. Injections must neither run inside the window nor
# start late enough that injection+recovery bleeds across midnight, so starts
# are deferred from 23:20 KST (max injection 30m + recovery margin) until the
# dump has landed at 02:30 KST.
PROTECTION_WINDOW_START_KST = dt_time(23, 20)
PROTECTION_WINDOW_END_KST = dt_time(2, 30)
# Functional readiness (capture self-check, lucida login, preflight signals)
# proves live connectivity, so a green result is trusted for a short TTL; any
# failure is re-probed on the next call for fast recovery.
FUNCTIONAL_READINESS_TTL_SEC = 300
# R6 preflight gate (spec-scenario-load R6, 2026-07-20): before injecting, the
# leading [t1-10m, t1] window must be clean. A dirty verdict re-checks every 5m;
# after PREFLIGHT_MAX_ATTEMPTS the scenario is skipped and the queue continues.
PREFLIGHT_RECHECK_INTERVAL = timedelta(minutes=5)
PREFLIGHT_MAX_ATTEMPTS = 3
PREFLIGHT_WINDOW_LEAD = timedelta(minutes=10)
# Scenario spacing contract v2 (spec-scenario-load R6, 2026-07-20): the old 2h
# inter-scenario clean window is replaced by a minimum 30m gap between the
# previous t2 and the next t1, with the 2-layer preflight gate deciding
# cleanliness of the [t1-10m, t1] window.
CLEAN_WINDOW = timedelta(minutes=30)
# Mode-aware retry wait (2026-07-20): a calibration retry after a short, cleanly
# recovered failure does not need the full evaluation-grade 2h lead-in purity.
RETRY_CLEAN_WINDOW_CALIBRATION = timedelta(minutes=30)
RETRY_SHORT_INJECTION = timedelta(minutes=5)
MAX_TRANSIENT_AUTO_RETRIES = 2
TRANSIENT_AUTO_RETRY_REASONS = frozenset(
    {"safety_observation_unavailable", "decision_observation_unavailable"}
)
# profile apply failures are mechanical transients (kubectl conflict/timeout classes);
# reason carries a detail suffix, so these are matched by prefix. Seen: F09-P run
# 2333e298 paused overnight-capable queue on a one-shot kubectl conflict (2026-07-20).
TRANSIENT_AUTO_RETRY_PREFIXES = ("profile_apply_failed:",)

QueuePhase = Literal[
    "idle",
    "running",
    "waiting_capture",
    "waiting_clean_window",
    "paused",
    "completed",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LiveQueueState(StrictModel):
    schema_version: Literal[1] = 1
    queue_id: str | None = None
    scenario_ids: list[str] = Field(default_factory=lambda: list(LIVE_SCENARIO_ORDER))
    phase: QueuePhase = "idle"
    next_index: int = Field(default=0, ge=0)
    current_scenario_id: str | None = None
    current_run_id: str | None = None
    completed_run_ids: list[str] = Field(default_factory=list)
    auto_retry_counts: dict[str, int] = Field(default_factory=dict)
    pending_retry_capture_run_id: str | None = None
    last_auto_retry_reason: str | None = None
    first_gate_passed: bool = False
    catalog_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    clean_window_not_before: str | None = None
    # R6 preflight gate state for the scenario currently awaiting injection.
    preflight_attempts: int = Field(default=0, ge=0, le=PREFLIGHT_MAX_ATTEMPTS)
    preflight_retry_not_before: str | None = None
    skipped_scenario_ids: list[str] = Field(default_factory=list)
    reason: str | None = None
    started_at: str | None = None
    updated_at: str | None = None

    @model_validator(mode="after")
    def fixed_sequence(self) -> "LiveQueueState":
        if set(self.skipped_scenario_ids) - set(self.scenario_ids):
            raise ValueError("skipped scenarios must reference queued scenarios")
        if self.scenario_ids[: len(LIVE_SCENARIO_ORDER)] != list(LIVE_SCENARIO_ORDER):
            raise ValueError("live queue must preserve the approved eight-scenario prefix")
        if len(self.scenario_ids) != len(set(self.scenario_ids)):
            raise ValueError("live queue scenario ids must be unique")
        if len(self.completed_run_ids) > self.next_index:
            raise ValueError("completed runs cannot exceed the queue cursor")
        if set(self.auto_retry_counts) - set(self.scenario_ids):
            raise ValueError("auto retry counts must reference queued scenarios")
        if any(
            isinstance(count, bool) or count < 0 or count > MAX_TRANSIENT_AUTO_RETRIES
            for count in self.auto_retry_counts.values()
        ):
            raise ValueError("auto retry count is outside the approved bound")
        if self.queue_id is not None and self.catalog_snapshot_sha256 is None:
            raise ValueError("active queue requires a manifest snapshot digest")
        return self


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class PreflightProbe(Protocol):
    """Fetch the R6 deterministic signals for the leading [t1-10m, t1] window.

    Production implementations read the existing observation adapters
    (loadgen_summary / http_probe / prometheus / database / alarm). The returned
    mapping is fed to ``build_preflight_checks``; it must contain every key in
    ``preflight.R6_REQUIRED_SIGNALS`` or the gate fails closed.
    """

    def collect(self, *, now: datetime) -> Mapping[str, float]: ...


class OperationalReadiness(StrictModel):
    ready: bool
    checks: dict[str, bool]
    missing: list[str]


class LiveScenarioQueue:
    """Run the fixed live sequence, never advancing past weak evidence."""

    def __init__(
        self,
        runner: ScenarioRunner,
        state_path: Path,
        *,
        clock: Clock | None = None,
        poll_interval_sec: float = QUEUE_POLL_INTERVAL_SEC,
        required_paths: dict[str, Path] | None = None,
        required_env: tuple[str, ...] = (
            "COMMERCE_DB_PASSWORD",
            "PG_PASSWORD",
            "CH_PASSWORD",
            "MODEL_SNAPSHOT_SSH_TARGET",
        ),
        scenario_registry_path: Path | None = None,
        manifest_root: Path | None = None,
        preflight_probe: "PreflightProbe | None" = None,
        preflight_ai_judge: AiJudge | None = None,
        functional_readiness_enabled: bool = False,
        golden_reset_enabled: bool = False,
        golden_store_root: Path | None = None,
        restore_script: Path | None = None,
        restore_runner: "ProcessRunner | None" = None,
    ) -> None:
        self.runner = runner
        self.state_path = state_path
        self.clock = clock or SystemClock()
        self.poll_interval_sec = poll_interval_sec
        # When no probe is wired the R6 preflight gate is inert (older callers).
        self.preflight_probe = preflight_probe
        self.preflight_ai_judge = preflight_ai_judge
        # Opt-in (production only): live end-to-end dependency proof at queue
        # start — unit tests keep the cheap path.
        self.functional_readiness_enabled = functional_readiness_enabled
        self.golden_reset_enabled = golden_reset_enabled
        self.golden_store_root = golden_store_root or Path(
            os.environ.get("GOLDEN_STORE_ROOT", "/data/eval-cases/goldens")
        )
        self.restore_script = restore_script or Path(
            os.environ.get("RESTORE_SCRIPT", "/opt/lucida/restore-golden-state.sh")
        )
        self._restore_runner = restore_runner or subprocess.run
        self.required_paths = required_paths or {
            "kubeconfig": Path("/root/tb-kubeconfig"),
            "ssh_key": Path("/root/.ssh/tb_key"),
            "ssh_known_hosts": Path("/root/.ssh/known_hosts"),
            "trusted_dispatcher": runner.dispatcher_path,
            "trusted_catalog": runner.catalog_path,
            "capture_script": runner.capture_scheduler.invoker.capture_script,
            "docker_socket": Path("/var/run/docker.sock"),
        }
        self.required_env = required_env
        self.scenario_registry_path = scenario_registry_path
        self.manifest_root = manifest_root
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._functional_cache: tuple[datetime, dict[str, bool]] | None = None

    def snapshot(self) -> LiveQueueState:
        if not self.state_path.is_file():
            return LiveQueueState()
        return LiveQueueState.model_validate_json(self.state_path.read_text(encoding="utf-8"))

    def readiness(self) -> OperationalReadiness:
        checks: dict[str, bool] = {}
        for name, path in self.required_paths.items():
            if name == "docker_socket":
                checks[name] = path.exists() and stat.S_ISSOCK(path.stat().st_mode)
            else:
                checks[name] = path.is_file() and os.access(path, os.R_OK)
        for name in self.required_env:
            checks[f"env:{name}"] = bool(os.environ.get(name))
        try:
            self._queue_contract()
            checks["live_manifest_snapshot"] = True
        except Exception:
            checks["live_manifest_snapshot"] = False
        coordinator = self.runner.coordinator.snapshot()
        checks["coordinator_clean"] = (
            coordinator.active_lease is None and coordinator.dirty_run is None
        )
        checks["runner_idle"] = not self.runner.is_busy
        checks.update(self._functional_readiness())
        missing = sorted(name for name, passed in checks.items() if not passed)
        return OperationalReadiness(ready=not missing, checks=checks, missing=missing)

    def _functional_readiness(self) -> dict[str, bool]:
        """Prove — not assume — the full pipeline before any scenario is spent.

        Root fix for the capture-time surprise lineage (inert preflight.json,
        missing QUERY_API_PASSWORD, 2026-07-21): every dependency any later
        stage needs is exercised live at queue start. Green results are cached
        for FUNCTIONAL_READINESS_TTL_SEC; any failure re-probes on next call.
        """
        if not self.functional_readiness_enabled:
            return {}
        now = self.clock.now()
        if self._functional_cache is not None:
            checked_at, cached = self._functional_cache
            fresh = (now - checked_at).total_seconds() < FUNCTIONAL_READINESS_TTL_SEC
            if fresh and all(cached.values()):
                return dict(cached)
        checks: dict[str, bool] = {}
        script = self.required_paths["capture_script"]
        try:
            result = subprocess.run(
                [str(script), "--self-check"],
                capture_output=True, text=True, timeout=120,
            )
            checks["capture_self_check"] = result.returncode == 0
        except Exception:
            checks["capture_self_check"] = False
        try:
            from app.incident_close import open_incident_count

            open_incident_count()
            checks["lucida_incident_api"] = True
        except Exception:
            checks["lucida_incident_api"] = False
        if self.preflight_probe is not None:
            try:
                observations = self.preflight_probe.collect(now=now)
                build_preflight_checks(observations)
                checks["preflight_signals"] = True
            except Exception:
                checks["preflight_signals"] = False
        self._functional_cache = (now, dict(checks))
        return checks

    async def start(self) -> LiveQueueState:
        async with self._lock:
            current = self.snapshot()
            if current.phase in {"running", "waiting_capture", "waiting_clean_window"}:
                raise RuntimeError("live queue is already active")
            if current.phase == "paused":
                raise RuntimeError("live queue is paused; fix the blocker and use resume")
            readiness = self.readiness()
            if not readiness.ready:
                raise RuntimeError(f"live queue readiness failed: {', '.join(readiness.missing)}")
            now = self._format(self.clock.now())
            scenario_ids, snapshot_sha256 = self._queue_contract()
            state = LiveQueueState(
                queue_id=f"live-{len(scenario_ids)}-{uuid.uuid4().hex[:8]}",
                scenario_ids=scenario_ids,
                catalog_snapshot_sha256=snapshot_sha256,
                phase="running",
                started_at=now,
                updated_at=now,
            )
            self._write(state)
        self.ensure_worker()
        return self.snapshot()

    async def resume(self) -> LiveQueueState:
        async with self._lock:
            state = self.snapshot()
            if state.phase != "paused":
                raise RuntimeError("only a paused live queue can be resumed")
            readiness = self.readiness()
            if not readiness.ready:
                raise RuntimeError(f"live queue readiness failed: {', '.join(readiness.missing)}")
            current_run_id = state.current_run_id
            capture_job = self._capture_job(current_run_id) if current_run_id else None
            previous_t2 = self._previous_t2(current_run_id)
            update = {
                "phase": "running",
                "current_scenario_id": None,
                "current_run_id": None,
                "clean_window_not_before": None,
                "reason": None,
                "updated_at": self._format(self.clock.now()),
            }
            scenario_ids, snapshot_sha256 = self._queue_contract()
            if scenario_ids != state.scenario_ids:
                raise RuntimeError("live queue repair cannot change the frozen scenario list")
            update["catalog_snapshot_sha256"] = snapshot_sha256
            capture_can_resume = (
                capture_job is not None
                and capture_job.status in {"pending", "model_requested", "completed"}
                and current_run_id is not None
                and self._controller_evidence_error(current_run_id) is None
            )
            if capture_can_resume:
                update.update(
                    {
                        "phase": "waiting_capture",
                        "current_scenario_id": state.current_scenario_id,
                        "current_run_id": current_run_id,
                    }
                )
            elif previous_t2 is not None:
                resume_window = CLEAN_WINDOW
                if state.current_scenario_id and current_run_id:
                    resume_window = self._retry_clean_window(
                        state.current_scenario_id, current_run_id
                    )
                update.update(
                    {
                        "phase": "waiting_clean_window",
                        "clean_window_not_before": self._format(previous_t2 + resume_window),
                    }
                )
            state = state.model_copy(update=update)
            self._write(state)
        if state.phase == "waiting_capture":
            self.runner.ensure_capture_worker()
        self.ensure_worker()
        return self.snapshot()

    async def append_promoted(self) -> LiveQueueState:
        """Append newly approved live scenarios without changing queued work."""
        async with self._lock:
            state = self.snapshot()
            if state.queue_id is None or state.phase == "completed":
                raise RuntimeError("an active live queue is required")
            scenario_ids, snapshot_sha256 = self._queue_contract()
            if scenario_ids[: len(state.scenario_ids)] != state.scenario_ids:
                raise RuntimeError("promoted scenarios must append to the frozen queue")
            state = state.model_copy(
                update={
                    "scenario_ids": scenario_ids,
                    "catalog_snapshot_sha256": snapshot_sha256,
                    "updated_at": self._format(self.clock.now()),
                }
            )
            self._write(state)
        self.ensure_worker()
        return self.snapshot()

    async def tick(self) -> LiveQueueState:
        async with self._lock:
            state = self.snapshot()
            if state.phase in {"idle", "paused", "completed"}:
                return state
            state = self._append_promoted_if_available(state)
            if self.runner.coordinator.snapshot().dirty_run is not None:
                return self._pause(state, "DIRTY coordinator state blocks the live queue")
            if state.phase == "running":
                return await self._tick_running(state)
            if state.phase == "waiting_capture":
                return self._tick_capture(state)
            return self._tick_clean_window(state)

    def _append_promoted_if_available(self, state: LiveQueueState) -> LiveQueueState:
        """Discover valid append-only promotions without operator intervention.

        Registry/manifests may be updated non-atomically during deployment, so an
        incomplete snapshot is retried on the next poll rather than pausing an
        otherwise healthy live run.
        """
        try:
            scenario_ids, snapshot_sha256 = self._queue_contract()
        except Exception:
            return state
        if scenario_ids[: len(state.scenario_ids)] != state.scenario_ids:
            return state
        if len(scenario_ids) == len(state.scenario_ids):
            return state
        state = state.model_copy(
            update={
                "scenario_ids": scenario_ids,
                "catalog_snapshot_sha256": snapshot_sha256,
                "updated_at": self._format(self.clock.now()),
            }
        )
        self._write(state)
        return state

    async def _tick_running(self, state: LiveQueueState) -> LiveQueueState:
        if state.current_run_id is None:
            if state.next_index >= len(state.scenario_ids):
                return self._complete(state)
            if self.runner.is_busy or self.runner.coordinator.snapshot().active_lease is not None:
                return state
            scenario_id = state.scenario_ids[state.next_index]
            # Daily normal-segment protection: never let an injection start
            # inside — or bleed into — the 00:00-02:00 KST clean window.
            deferral = self._protection_window_reason(self.clock.now())
            if deferral is not None:
                if state.reason == deferral:
                    return state
                deferred = state.model_copy(
                    update={"reason": deferral, "updated_at": self._format(self.clock.now())}
                )
                self._write(deferred)
                return deferred
            # R6 preflight gate: prove [t1-10m, t1] is clean before injecting.
            short_circuit, verdict = self._preflight_gate(state, scenario_id)
            if short_circuit is not None:
                return short_circuit
            try:
                self._restore_golden(scenario_id)
            except Exception as error:
                return self._pause(state, f"golden restore failed for {scenario_id}: {error}")
            try:
                run = await self.runner.start(scenario_id=scenario_id, mode="run")
            except Exception as error:
                return self._pause(state, f"start failed for {scenario_id}: {error}")
            if verdict is not None:
                self._write_preflight(run.run_id, verdict)
            self._write_normal_segment_marker(run.run_id)
            state = state.model_copy(
                update={
                    "current_scenario_id": scenario_id,
                    "current_run_id": run.run_id,
                    "preflight_attempts": 0,
                    "preflight_retry_not_before": None,
                    "reason": None,
                    "updated_at": self._format(self.clock.now()),
                }
            )
            self._write(state)
            return state

        current = self.runner.get_current()
        if current is not None and current.run_id == state.current_run_id:
            if current.status in {"running", "cleanup_running"}:
                return state
            if current.dirty:
                return self._pause(state, f"DIRTY run: {current.run_id}")
            if current.status != "succeeded":
                return self._pause(state, f"scenario evidence failed: {current.run_id}")
        elif self.runner.coordinator.snapshot().active_lease is not None:
            return state

        evidence_error = self._controller_evidence_error(state.current_run_id)
        if evidence_error is not None:
            retry_reason = self._controller_retry_reason(state.current_run_id)
            retries = state.auto_retry_counts.get(state.current_scenario_id or "", 0)
            previous_t2 = self._previous_t2(state.current_run_id)
            if (
                retry_reason is not None
                and (
                    retry_reason in TRANSIENT_AUTO_RETRY_REASONS
                    or retry_reason.startswith(TRANSIENT_AUTO_RETRY_PREFIXES)
                )
                and state.current_scenario_id is not None
                and retries < MAX_TRANSIENT_AUTO_RETRIES
                and previous_t2 is not None
            ):
                counts = dict(state.auto_retry_counts)
                counts[state.current_scenario_id] = retries + 1
                retry_window = self._retry_clean_window(
                    state.current_scenario_id, state.current_run_id
                )
                state = state.model_copy(
                    update={
                        "phase": "waiting_clean_window",
                        "current_scenario_id": None,
                        "current_run_id": None,
                        "clean_window_not_before": self._format(
                            previous_t2 + retry_window
                        ),
                        "auto_retry_counts": counts,
                        "pending_retry_capture_run_id": None,
                        "last_auto_retry_reason": retry_reason,
                        "reason": None,
                        "updated_at": self._format(self.clock.now()),
                    }
                )
                self._write(state)
                return state
            return self._pause(state, evidence_error)
        job = self._capture_job(state.current_run_id)
        if job is None:
            return self._pause(state, f"capture was not scheduled: {state.current_run_id}")
        state = state.model_copy(
            update={"phase": "waiting_capture", "updated_at": self._format(self.clock.now())}
        )
        self._write(state)
        return state

    def _tick_capture(self, state: LiveQueueState) -> LiveQueueState:
        assert state.current_run_id is not None
        job = self._capture_job(state.current_run_id)
        if job is None:
            return self._pause(state, f"capture job disappeared: {state.current_run_id}")
        if job.status == "failed":
            return self._pause(state, f"capture failed: {state.current_run_id}: {job.failure}")
        if job.status != "completed":
            return state
        if not (self.runner.artifact_store.root / state.current_run_id / "capture-complete.json").is_file():
            return self._pause(state, f"capture completion evidence missing: {state.current_run_id}")
        self._thaw_trainer()
        completed = [*state.completed_run_ids, state.current_run_id]
        next_index = state.next_index + 1
        first_gate = state.first_gate_passed or state.next_index == 0
        if next_index >= len(state.scenario_ids):
            return self._complete(
                state.model_copy(
                    update={
                        "completed_run_ids": completed,
                        "next_index": next_index,
                        "first_gate_passed": first_gate,
                    }
                )
            )
        t2 = parse_utc(job.t2, field="t2")
        state = state.model_copy(
            update={
                "phase": "waiting_clean_window",
                "next_index": next_index,
                "completed_run_ids": completed,
                "first_gate_passed": first_gate,
                "current_scenario_id": None,
                "current_run_id": None,
                "clean_window_not_before": self._format(t2 + CLEAN_WINDOW),
                "updated_at": self._format(self.clock.now()),
            }
        )
        self._write(state)
        return state

    def _tick_clean_window(self, state: LiveQueueState) -> LiveQueueState:
        if state.clean_window_not_before is None:
            return self._pause(state, "clean-window deadline is missing")
        if self.clock.now().astimezone(timezone.utc) < parse_utc(
            state.clean_window_not_before, field="clean_window_not_before"
        ):
            return state
        if state.pending_retry_capture_run_id is not None:
            job = self._capture_job(state.pending_retry_capture_run_id)
            if job is None:
                return self._pause(
                    state,
                    f"retry capture disappeared: {state.pending_retry_capture_run_id}",
                )
            if job.status == "failed":
                return self._pause(
                    state,
                    f"retry capture failed: {job.run_id}: {job.failure}",
                )
            if job.status != "completed":
                return state
            evidence = (
                self.runner.artifact_store.root
                / state.pending_retry_capture_run_id
                / "capture-complete.json"
            )
            if not evidence.is_file():
                return self._pause(
                    state,
                    f"retry capture completion evidence missing: {job.run_id}",
                )
        readiness = self.readiness()
        if not readiness.ready:
            blockers = set(readiness.missing) - {"coordinator_clean", "runner_idle"}
            if blockers:
                return self._pause(state, f"operational readiness failed: {', '.join(sorted(blockers))}")
            return state
        state = state.model_copy(
            update={
                "phase": "running",
                "clean_window_not_before": None,
                "pending_retry_capture_run_id": None,
                "updated_at": self._format(self.clock.now()),
            }
        )
        self._write(state)
        return state

    def _preflight_gate(
        self, state: LiveQueueState, scenario_id: str
    ) -> tuple[LiveQueueState | None, PreflightVerdict | None]:
        """Decide whether ``scenario_id`` may inject now (R6).

        Returns ``(None, verdict)`` to proceed with injection (drop ``verdict``
        after start), or ``(new_state, None)`` to short-circuit this tick because
        the queue is waiting for a recheck, has skipped the scenario, or paused.
        With no probe wired the gate is inert: ``(None, None)``.
        """
        if self.preflight_probe is None:
            return None, None
        now = self.clock.now().astimezone(timezone.utc)
        if state.preflight_retry_not_before is not None and now < parse_utc(
            state.preflight_retry_not_before, field="preflight_retry_not_before"
        ):
            return state, None
        waited_sec = state.preflight_attempts * int(
            PREFLIGHT_RECHECK_INTERVAL.total_seconds()
        )
        try:
            verdict = self._evaluate_preflight(now, waited_sec)
        except Exception as error:
            return self._pause(state, f"preflight probe failed for {scenario_id}: {error}"), None
        if verdict.is_clean:
            return None, verdict
        # Clean-slate sweep (2026-07-21 policy): when lingering incident
        # objects are the ONLY hard failure, closing them IS the R6 remedy —
        # capture-end auto-close cannot reach incidents left by failed runs or
        # opened organically between scenarios, and without this sweep they
        # starve the gate into a 30-scenario skip cascade. Any other failing
        # signal (5xx, p95, dead loadgen, firing alarms) still blocks.
        if self._sweep_lingering_incidents(verdict):
            try:
                verdict = self._evaluate_preflight(now, waited_sec)
            except Exception as error:
                return self._pause(state, f"preflight probe failed for {scenario_id}: {error}"), None
            if verdict.is_clean:
                return None, verdict

        attempts = state.preflight_attempts + 1
        if attempts >= PREFLIGHT_MAX_ATTEMPTS:
            advanced = state.model_copy(
                update={
                    "next_index": state.next_index + 1,
                    "skipped_scenario_ids": [*state.skipped_scenario_ids, scenario_id],
                    "preflight_attempts": 0,
                    "preflight_retry_not_before": None,
                    "reason": f"preflight timeout: skipped {scenario_id} ({verdict.verdict})",
                    "updated_at": self._format(now),
                }
            )
            if advanced.next_index >= len(advanced.scenario_ids):
                return self._complete(advanced), None
            self._write(advanced)
            return advanced, None

        waiting = state.model_copy(
            update={
                "preflight_attempts": attempts,
                "preflight_retry_not_before": self._format(now + PREFLIGHT_RECHECK_INTERVAL),
                "reason": (
                    f"preflight not clean ({verdict.verdict}); "
                    f"recheck {attempts}/{PREFLIGHT_MAX_ATTEMPTS}"
                ),
                "updated_at": self._format(now),
            }
        )
        self._write(waiting)
        return waiting, None

    def _evaluate_preflight(self, now: datetime, waited_sec: int) -> PreflightVerdict:
        assert self.preflight_probe is not None
        observations = self.preflight_probe.collect(now=now)
        checks = build_preflight_checks(observations)
        window = (self._format(now - PREFLIGHT_WINDOW_LEAD), self._format(now))
        return evaluate_preflight(
            window=window,
            checks=checks,
            now=now,
            waited_sec=waited_sec,
            ai_judge=self.preflight_ai_judge,
        )

    def _sweep_lingering_incidents(self, verdict: PreflightVerdict) -> bool:
        failed = [check for check in verdict.checks if check.status == "fail"]
        if not failed or any(check.name != "open_incidents" for check in failed):
            return False
        try:
            from app.incident_close import close_open_incidents

            close_open_incidents()
            return True
        except Exception:
            return False

    def _run_restore(self, args: list[str], *, timeout: int = 900) -> None:
        target = os.environ.get("MODEL_SNAPSHOT_SSH_TARGET")
        if not target:
            raise RuntimeError("MODEL_SNAPSHOT_SSH_TARGET is required for golden reset")
        ssh_key = self.required_paths["ssh_key"]
        creds = []
        for name in ("PG_PASSWORD", "PG_USER", "CH_PASSWORD", "LUCIDA_LOGIN_USER", "LUCIDA_LOGIN_PASSWORD"):
            value = os.environ.get(name)
            if value:
                creds.append(f"{name}={shlex.quote(value)}")
        remote = " ".join([*creds, "bash", shlex.quote(str(self.restore_script)), *(shlex.quote(a) for a in args)])
        self._restore_runner(
            ["ssh", "-i", str(ssh_key), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
             "-o", "ConnectTimeout=10", target, remote],
            check=True, capture_output=True, text=True, timeout=timeout,
        )

    def _restore_golden(self, scenario_id: str) -> None:
        """Freeze trainer + reset AI DB state + restart observer to a clean golden
        before injecting scenario_id. No-op unless golden reset is enabled."""
        if not self.golden_reset_enabled:
            return
        self._run_restore(["--golden-root", str(self.golden_store_root), "--for-time", "now"])

    def _thaw_trainer(self) -> None:
        """Un-freeze the trainer. Fail-open: never block the queue on thaw."""
        if not self.golden_reset_enabled:
            return
        with suppress(Exception):
            self._run_restore(["--thaw"], timeout=60)

    def _write_preflight(self, run_id: str, verdict: PreflightVerdict) -> None:
        """Drop the verdict where ProductionCaptureInvoker forwards it to capture."""
        path = self.runner.artifact_store.root / run_id / "preflight.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(verdict.to_meta(), stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(raw, path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(raw)

    @staticmethod
    def _protection_window_reason(now: datetime) -> str | None:
        """Defer injection starts around the daily 00:00-02:00 KST clean window.

        Starts are blocked from 23:20 KST (worst-case injection 30m + recovery
        would cross midnight and poison the day's shared normal prefix) until
        02:30 KST (the 02:20 host cron has landed the dump by then).
        """
        kst = now.astimezone(ZoneInfo("Asia/Seoul")).time()
        if kst >= PROTECTION_WINDOW_START_KST or kst < PROTECTION_WINDOW_END_KST:
            return (
                "daily normal-segment protection window (23:20-02:30 KST): "
                "injection start deferred"
            )
        return None

    def _write_normal_segment_marker(self, run_id: str) -> None:
        """Point capture at today's shared daily normal segment (contract v2).

        The daily 00:00-02:00 KST dump is produced out-of-band (host cron on
        109); this only records where today's segment lives so the capture
        invoker forwards it as --normal-segment and builds assembled/.
        Fail-open: no dump for today means no marker and no assembled/.
        """
        root = os.environ.get("NORMAL_SEGMENT_ROOT")
        if not root:
            return
        domain = os.environ.get("NORMAL_SEGMENT_DOMAIN", "commerce")
        date_kst = self.clock.now().astimezone(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")
        segment = Path(root) / domain / date_kst
        if not (segment / "meta.json").is_file():
            return
        path = self.runner.artifact_store.root / run_id / "normal-segment.path"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{segment}\n", encoding="utf-8")

    def _controller_evidence_error(self, run_id: str) -> str | None:
        state_path = self.runner.artifact_store.root / run_id / "state.json"
        result_path = self.runner.artifact_store.root / run_id / "result.json"
        if not state_path.is_file() or not result_path.is_file():
            return f"controller evidence missing: {run_id}"
        try:
            session = ControllerSession.model_validate_json(state_path.read_text(encoding="utf-8"))
        except Exception as error:
            return f"controller evidence invalid: {run_id}: {type(error).__name__}"
        if session.status != SessionStatus.CLEAN:
            return f"controller did not recover cleanly: {run_id}: {session.status.value}"
        if session.controller_state is None or session.controller_state.phase != ControllerPhase.SUCCEEDED:
            phase = session.controller_state.phase.value if session.controller_state else "missing"
            return f"controller success evidence failed: {run_id}: {phase}"
        return None

    def _controller_retry_reason(self, run_id: str) -> str | None:
        state_path = self.runner.artifact_store.root / run_id / "state.json"
        if not state_path.is_file():
            return None
        try:
            session = ControllerSession.model_validate_json(
                state_path.read_text(encoding="utf-8")
            )
        except Exception:
            return None
        if session.controller_state is None:
            return None
        return session.controller_state.reason

    def _retry_clean_window(self, scenario_id: str, run_id: str) -> timedelta:
        """Shortened wait only when every excuse condition is provably true (fail-closed)."""
        try:
            registry = json.loads(Path(self.scenario_registry_path).read_text(encoding="utf-8"))
            if registry["controllers"][scenario_id].get("mode") != "calibration":
                return CLEAN_WINDOW
            result = json.loads(
                (self.runner.artifact_store.root / run_id / "result.json").read_text(encoding="utf-8")
            )
            if result.get("dirty") is not False:
                return CLEAN_WINDOW
            if result.get("cleanup", {}).get("status") != "succeeded":
                return CLEAN_WINDOW
            changes = result.get("level_changes") or []
            starts = [c.get("applied_at") for c in changes if c.get("applied_at")]
            ends = [c.get("effect_ended_at") for c in changes if c.get("effect_ended_at")]
            if not starts or len(ends) != len(starts):
                return CLEAN_WINDOW
            span = parse_utc(max(ends), field="effect_ended_at") - parse_utc(
                min(starts), field="applied_at"
            )
            if span > RETRY_SHORT_INJECTION:
                return CLEAN_WINDOW
        except Exception:
            return CLEAN_WINDOW
        marker = self.runner.artifact_store.root / run_id / "clean-window-excused.json"
        marker.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "scenario_id": scenario_id,
                    "reason": "calibration-retry-short-injection",
                    "injection_span_sec": int(span.total_seconds()),
                    "excused_at": self._format(self.clock.now()),
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return RETRY_CLEAN_WINDOW_CALIBRATION

    def _capture_job(self, run_id: str) -> CaptureJob | None:
        return self.runner.capture_scheduler.snapshot().jobs.get(run_id)

    def _previous_t2(self, run_id: str | None) -> datetime | None:
        if run_id is None:
            return None
        job = self._capture_job(run_id)
        if job is not None:
            return parse_utc(job.t2, field="t2")
        path = self.runner.artifact_store.root / run_id / "state.json"
        if not path.is_file():
            return None
        try:
            session = ControllerSession.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return session.t2

    def _queue_contract(self) -> tuple[list[str], str]:
        """Freeze the current approved list; later registry edits cannot alter this queue."""
        if self.scenario_registry_path is None and self.manifest_root is None:
            encoded = json.dumps(list(LIVE_SCENARIO_ORDER), separators=(",", ":")).encode()
            return list(LIVE_SCENARIO_ORDER), hashlib.sha256(encoded).hexdigest()
        if self.scenario_registry_path is None or self.manifest_root is None:
            raise RuntimeError("live registry and manifest root must be configured together")
        registry_bytes = self.scenario_registry_path.read_bytes()
        registry = json.loads(registry_bytes)
        scenario_ids = registry.get("live_scenario_ids")
        if not isinstance(scenario_ids, list) or not all(
            isinstance(item, str) and item for item in scenario_ids
        ):
            raise RuntimeError("live controller registry has no ordered scenario list")
        if scenario_ids[: len(LIVE_SCENARIO_ORDER)] != list(LIVE_SCENARIO_ORDER):
            raise RuntimeError("live controller registry changed the approved eight-scenario prefix")
        if len(scenario_ids) != len(set(scenario_ids)):
            raise RuntimeError("live controller registry contains duplicate scenario ids")
        manifests: dict[str, bytes] = {}
        for path in sorted([*self.manifest_root.glob("*.yaml"), *self.manifest_root.glob("*.yml")]):
            content = path.read_bytes()
            document = json.loads(content)
            manifest_id = document.get("id")
            if isinstance(manifest_id, str):
                manifests[manifest_id] = content
        digest = hashlib.sha256()
        digest.update(registry_bytes)
        for scenario_id in scenario_ids:
            load_scenario_metadata(self.runner.scenario_metadata_path, scenario_id)
            content = manifests.get(scenario_id)
            if content is None:
                raise RuntimeError(f"live manifest is missing: {scenario_id}")
            document = json.loads(content)
            controller = document.get("execution", {}).get("controller", {})
            if controller.get("live_enabled") is not True:
                raise RuntimeError(f"live manifest is not enabled: {scenario_id}")
            digest.update(scenario_id.encode())
            digest.update(content)
        return list(scenario_ids), digest.hexdigest()

    def ensure_worker(self) -> None:
        state = self.snapshot()
        if state.phase in {"running", "waiting_capture", "waiting_clean_window"} and (
            self._task is None or self._task.done()
        ):
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while self.snapshot().phase in {"running", "waiting_capture", "waiting_clean_window"}:
            try:
                await self.tick()
            except Exception as error:
                async with self._lock:
                    self._pause(self.snapshot(), f"queue worker failed: {type(error).__name__}: {error}")
            await asyncio.sleep(self.poll_interval_sec)

    async def stop_worker(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def _pause(self, state: LiveQueueState, reason: str) -> LiveQueueState:
        self._thaw_trainer()
        state = state.model_copy(
            update={"phase": "paused", "reason": reason, "updated_at": self._format(self.clock.now())}
        )
        self._write(state)
        return state

    def _complete(self, state: LiveQueueState) -> LiveQueueState:
        self._thaw_trainer()
        state = state.model_copy(
            update={
                "phase": "completed",
                "current_scenario_id": None,
                "current_run_id": None,
                "clean_window_not_before": None,
                "pending_retry_capture_run_id": None,
                "reason": None,
                "updated_at": self._format(self.clock.now()),
            }
        )
        self._write(state)
        return state

    @staticmethod
    def _format(value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("queue clock must be timezone-aware")
        return value.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _write(self, state: LiveQueueState) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        fd, raw = tempfile.mkstemp(prefix=f".{self.state_path.name}.", dir=self.state_path.parent)
        temporary = Path(raw)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(state.model_dump_json(indent=2))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o640)
            os.replace(temporary, self.state_path)
        finally:
            temporary.unlink(missing_ok=True)


_queue: LiveScenarioQueue | None = None


def get_live_queue() -> LiveScenarioQueue:
    global _queue
    if _queue is None:
        runner = get_runner()
        state_path = Path(os.environ.get("LIVE_QUEUE_STATE_PATH", "/app/state/live-queue.json"))
        registry_path = Path(
            os.environ.get(
                "LIVE_CONTROLLER_REGISTRY_PATH",
                "/opt/lucida/scenario-contracts/registry/controllers.json",
            )
        )
        manifest_root = Path(
            os.environ.get("SCENARIO_MANIFEST_ROOT", "/app/scenario-manifests")
        )
        preflight_probe = None
        if os.environ.get("PREFLIGHT_PROBE", "on").lower() not in {"off", "0", "disabled"}:
            from app.preflight_probe import ProductionPreflightProbe

            preflight_probe = ProductionPreflightProbe()
        _queue = LiveScenarioQueue(
            runner,
            state_path,
            scenario_registry_path=registry_path,
            manifest_root=manifest_root,
            preflight_probe=preflight_probe,
            functional_readiness_enabled=True,
            golden_reset_enabled=os.environ.get("GOLDEN_RESET_ENABLED", "").lower()
            in {"1", "true", "yes", "on"},
        )
    return _queue
