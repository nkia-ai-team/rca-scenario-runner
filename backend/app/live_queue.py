"""Persistent, fail-closed orchestration for the approved live scenario sequence."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
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
from app.decoys import (
    ROLLOUT_KINDS,
    DecoyRecord,
    KubectlDecoyExecutor,
    build_schedule,
    cycle_seed,
    decoy_document,
    load_decoy_policy,
    partition_decoy_alarms,
)
from app.live_probes import _is_run_directory, _run_intervals
from app.pass_mode import (
    capture_enabled,
    golden_reset_enabled,
    inter_scenario_gap,
    isolation_checks_enabled,
    protection_window_enabled,
)
from app.preflight import (
    AiJudge,
    PreflightVerdict,
    build_preflight_checks,
    evaluate_preflight,
)
from app.runner import ScenarioRunner, get_runner


ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]

# Pauses and cycle restarts are the queue's operator-facing events. The root
# logger has no handler under uvicorn, so WARNING lands on stderr through
# logging.lastResort and therefore in `docker logs scenario-runner`.
logger = logging.getLogger("scenario_runner.live_queue")


# F03-G, F09-P and F11-G left this queue on 2026-07-27: the golden audit parked
# them (readiness=parked in testbed-services catalog.json), so compile-plan no
# longer yields live_allowed and the dispatcher would refuse them anyway.
LIVE_SCENARIO_ORDER = (
    "F01-R",
    "F01-H",
    "F06-R",
    "F07-H",
    "F08-H",
)
QUEUE_POLL_INTERVAL_SEC = 5
# restore-golden-state.sh result golden_sha256: sha256 of the restored golden dump.
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
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
# 6, not 3 (2026-08-04). A destructive predecessor leaves the gate looking at an
# empty window rather than a broken path: F25-H OOM-kills commerce PostgreSQL, so
# for the minutes it takes to restart no order is recorded, and F23-R's
# baseline-business-success saw 0/0/1 PAID over its leading 5m while the path
# itself was healthy (15m total: 2093 PAID). That is a wait, not a skip — but
# 3 x 5m expired before the window refilled and the run was lost. Six attempts
# cover a stateful restart plus the refill; a genuinely dead path still fails,
# just 15 minutes later, which is cheap next to forfeiting a dataset case.
PREFLIGHT_MAX_ATTEMPTS = 6
PREFLIGHT_WINDOW_LEAD = timedelta(minutes=10)
# The readiness gate's probe is transport, not judgement (2026-08-04 batch): a
# partial poll makes build_preflight_checks raise by design ("fails closed"), and
# one such hiccup used to pause the whole 44-scenario queue on the first tick —
# the per-scenario gate above already rechecks 6 x 5m for the same condition.
# Give the batch-level gate the same slack: keep waiting while the probe alone is
# the blocker, and pause only if it stays broken for this long.
READINESS_PROBE_GRACE = timedelta(minutes=3)
# capture_self_check joins the probe here for the same reason, but only ever
# appears at all on a dataset pass (see _functional_readiness): a smoke pass
# exports nothing, so the capture chain is not a dependency it can fail on.
TRANSIENT_READINESS_CHECKS = frozenset({"preflight_signals", "capture_self_check"})


def _tail(text: str, limit: int = 200) -> str:
    """Last line of a failed probe's output, short enough to live in `reason`."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1][:limit] if lines else "no output"
# Scenario spacing contract v2 (spec-scenario-load R6, 2026-07-20): the old 2h
# inter-scenario clean window is replaced by a minimum 30m gap between the
# previous t2 and the next t1, with the 2-layer preflight gate deciding
# cleanliness of the [t1-10m, t1] window.
#
# 30m is not a preference: the capture window is [t1-10m, t2+20m], so anything
# under POST_WINDOW + PRE_WINDOW overlaps the neighbouring case, and each
# manifest's baseline.clean_window enforces it again with a ge=1800 floor.
# Lowering only this value does not shorten a dataset pass — it just moves the
# rejection to the runner's isolation gates (measured 2026-07-31: 15m blocked
# every start with check_failed:clean-window + scenario_overlap).
#
# The gap is a property of the pass, so it lives in app.pass_mode.
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

# Continuous-cycle capture contract v3 (spec-eval-data-capture §2.2, 2026-07-24).
# A case == one cycle on a real continuous time axis:
#   cycle_reset -> cycle_normal(2h) -> cycle_buffer(10m) -> running[t1,t2]
#     -> cycle_cooldown(30m) -> waiting_capture([cycle_start, t2+30m]).
# Enabled only when CYCLE_MODE=1; otherwise the v2 daily-segment queue above is
# preserved unchanged.
#
# Which scenarios a cycle run covers is NOT decided here. Both queues read the
# one approved list in the controller registry, so a scenario promoted (or
# retired) is promoted for both. This used to be a hardcoded tuple of the first
# seven v3 subjects, which silently became a second, staler answer to "the
# catalogue" as the registry grew to 44 — a v3 capture would have covered seven
# and reported success, with nothing in the repo able to notice the other 37
# were missing. CYCLE_SCENARIOS still narrows a run for a targeted recapture,
# but only to scenarios the registry has already approved.
def _cycle_duration(env_name: str, default: timedelta) -> timedelta:
    """Contract v3 phase length, overridable in seconds for shortened smoke
    cycles (capture derives its window from phases.json, so shortened cycles
    stay internally consistent). Production leaves these unset."""
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    seconds = int(raw)
    if seconds <= 0:
        raise ValueError(f"{env_name} must be a positive integer of seconds: {raw}")
    return timedelta(seconds=seconds)


CYCLE_NORMAL_DURATION = _cycle_duration("CYCLE_NORMAL_SEC", timedelta(hours=2))
CYCLE_BUFFER_DURATION = _cycle_duration("CYCLE_BUFFER_SEC", timedelta(minutes=10))
CYCLE_COOLDOWN_DURATION = _cycle_duration("CYCLE_COOLDOWN_SEC", timedelta(minutes=30))
# Same-scenario cycle restarts are bounded to break infinite loops: two
# consecutive restarts are allowed, the third pauses (spec §2.2 resume table).
MAX_CYCLE_RESTARTS = 2

QueuePhase = Literal[
    "idle",
    "running",
    "waiting_capture",
    "waiting_clean_window",
    "paused",
    "completed",
    # v3 continuous-cycle phases (CYCLE_MODE only).
    "cycle_reset",
    "cycle_normal",
    "cycle_buffer",
    "cycle_cooldown",
]

# Phases in which the background worker loop must keep ticking.
ACTIVE_PHASES = frozenset(
    {
        "running",
        "waiting_capture",
        "waiting_clean_window",
        "cycle_reset",
        "cycle_normal",
        "cycle_buffer",
        "cycle_cooldown",
    }
)


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
    # --- v3 continuous-cycle state (CYCLE_MODE only; all default to the v2 no-op) ---
    cycle_mode: bool = False
    cycle_started_at: str | None = None
    cycle_buffer_started_at: str | None = None
    phase_started_at: str | None = None
    # Scheduled end of the current timed phase; monitoring uses this for stall
    # detection. None while injection runs (the controller watchdog owns that).
    expected_transition_at: str | None = None
    cycle_restart_counts: dict[str, int] = Field(default_factory=dict)
    cycle_golden_id: str | None = None
    cycle_golden_sha256: str | None = None
    # Decoy deployments planned for the current cycle (U7). Empty when the
    # feature is off or the cycle drew zero decoys; seed is None only when off.
    cycle_decoy_seed: int | None = None
    cycle_decoy_policy_sha256: str | None = None
    cycle_decoys: list[DecoyRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def fixed_sequence(self) -> "LiveQueueState":
        if set(self.skipped_scenario_ids) - set(self.scenario_ids):
            raise ValueError("skipped scenarios must reference queued scenarios")
        # The frozen eight-scenario prefix is a v2-queue invariant. The v3 cycle
        # queue runs its own (13-scenario) re-capture list, so the prefix check
        # is scoped to non-cycle queues.
        if (
            not self.cycle_mode
            and self.scenario_ids[: len(LIVE_SCENARIO_ORDER)] != list(LIVE_SCENARIO_ORDER)
        ):
            raise ValueError("live queue must preserve the approved eight-scenario prefix")
        if len(self.scenario_ids) != len(set(self.scenario_ids)):
            raise ValueError("live queue scenario ids must be unique")
        if set(self.cycle_restart_counts) - set(self.scenario_ids):
            raise ValueError("cycle restart counts must reference queued scenarios")
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
    # Why a functional check failed. The probes swallow their exception to keep a
    # boolean contract; without the text a paused batch says only which check
    # failed, never why (2026-08-04 batch: "preflight_signals" with no trace).
    details: dict[str, str] = Field(default_factory=dict)


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
        cycle_mode: bool = False,
        cycle_scenario_ids: tuple[str, ...] = (),
        cycle_health_probe: "Callable[[], str | None] | None" = None,
        topology_collector_factory: "Callable[[Path], object] | None" = None,
        decoy_policy_path: Path | None = None,
        decoy_executor: object | None = None,
        decoy_seed_base: str = "testbed-v4-decoy",
    ) -> None:
        self.runner = runner
        # v3 continuous-cycle mode (off by default; v2 behaviour preserved).
        self.cycle_mode = cycle_mode
        self.cycle_scenario_ids = cycle_scenario_ids
        # Returns an error string when the AI stack / stores are unhealthy during
        # the normal lead-in, else None. Default: always healthy (unit tests).
        self.cycle_health_probe = cycle_health_probe
        # Builds a per-cycle topology snapshot collector at a bundle dir. None
        # disables periodic topology capture (default; unit tests without it).
        self.topology_collector_factory = topology_collector_factory
        self._topology_collector: object | None = None
        # Decoy deployments (U7): cycle mode only, and only when a policy file is
        # wired. The executor is built from the policy unless a test injects one.
        self.decoy_policy_path = decoy_policy_path if cycle_mode else None
        self._decoy_executor = decoy_executor
        self.decoy_seed_base = decoy_seed_base
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
        # Settle delay before the single golden-restore retry; injected runners
        # (tests) get no artificial wait.
        self.restore_retry_delay_sec = 30 if restore_runner is None else 0
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
        # The loop that owns the topology collector task. Ticks run their
        # blocking work on worker threads, and those must hand loop-bound calls
        # (create_task) back to it.
        self._event_loop: asyncio.AbstractEventLoop | None = None
        # True while a tick has decided to restore or inject and is doing the
        # blocking part on a worker thread. The manual run route refuses
        # meanwhile: with the loop free, a web "run" used to land between the
        # queue's busy check and its own start (review R3).
        self.injection_reserved = False
        self._functional_cache: tuple[datetime, dict[str, bool]] | None = None
        self._functional_details: dict[str, str] = {}
        # First tick at which the probe alone blocked readiness; None while ready.
        self._probe_grace_since: datetime | None = None

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
            # Cycle mode freezes its own 13-scenario contract; the v2 live
            # registry contract must not gate it (registry edits for other
            # work would otherwise block the cycle queue).
            if self.cycle_mode:
                self._cycle_contract()
            else:
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
        details = {
            name: text
            for name, text in self._functional_details.items()
            if name in missing
        }
        return OperationalReadiness(
            ready=not missing, checks=checks, missing=missing, details=details
        )

    @staticmethod
    def _readiness_failure_reason(
        readiness: OperationalReadiness, blockers: set[str]
    ) -> str:
        names = sorted(blockers)
        reason = f"operational readiness failed: {', '.join(names)}"
        why = "; ".join(
            f"{name}: {readiness.details[name]}"
            for name in names
            if name in readiness.details
        )
        return f"{reason} ({why})" if why else reason

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
        details: dict[str, str] = {}
        # A smoke pass exports nothing (capture_enabled gates the work itself),
        # so proving the capture chain would only let an unused dependency stop
        # the batch — which is what happened on 2026-08-04, twice over a busy
        # 119. The pass rule allows exactly this: drop what only exists to make
        # or keep cases, never anything that decides pass/fail.
        if capture_enabled():
            script = self.required_paths["capture_script"]
            try:
                result = subprocess.run(
                    [str(script), "--self-check"],
                    capture_output=True, text=True, timeout=120,
                )
                checks["capture_self_check"] = result.returncode == 0
                if result.returncode != 0:
                    details["capture_self_check"] = _tail(
                        result.stderr or result.stdout
                    )
            except Exception as error:
                checks["capture_self_check"] = False
                details["capture_self_check"] = f"{type(error).__name__}: {error}"
        try:
            from app.incident_close import open_incident_count

            open_incident_count()
            checks["lucida_incident_api"] = True
        except Exception as error:
            checks["lucida_incident_api"] = False
            details["lucida_incident_api"] = f"{type(error).__name__}: {error}"
        if self.preflight_probe is not None:
            try:
                observations = self.preflight_probe.collect(now=now)
                build_preflight_checks(observations)
                checks["preflight_signals"] = True
            except Exception as error:
                checks["preflight_signals"] = False
                details["preflight_signals"] = f"{type(error).__name__}: {error}"
        self._functional_cache = (now, dict(checks))
        self._functional_details = details
        return checks

    async def start(self, scenario_ids: list[str] | None = None) -> LiveQueueState:
        """Start the queue. ``scenario_ids`` narrows a cycle run to those approved
        scenarios for this queue only, like CYCLE_SCENARIOS but without a container
        restart: the operation harness starts one candidate at a time this way
        (testbed-services docs/spec-scenario-lifecycle.md)."""
        if scenario_ids is not None and not self.cycle_mode:
            raise RuntimeError("scenario_ids is only supported in cycle mode")
        if scenario_ids is not None and not scenario_ids:
            raise RuntimeError("scenario_ids must name at least one scenario")
        async with self._lock:
            current = self.snapshot()
            if current.phase in ACTIVE_PHASES:
                raise RuntimeError("live queue is already active")
            if current.phase == "paused":
                raise RuntimeError("live queue is paused; fix the blocker and use resume")
            readiness = await asyncio.to_thread(self.readiness)
            if not readiness.ready:
                raise RuntimeError(f"live queue readiness failed: {', '.join(readiness.missing)}")
            now_at = self.clock.now()
            now = self._format(now_at)
            if self.cycle_mode:
                scenario_ids, snapshot_sha256 = self._cycle_contract(
                    tuple(scenario_ids) if scenario_ids is not None else None
                )
                # A new queue after a cancel (testbed-operate holds a scenario
                # with cancel, then starts the next) must not open its normal
                # window inside the tail of the last run either (review R2).
                settle_until = await asyncio.to_thread(self._recent_settle_not_before, now_at)
                reset_at = (
                    self._format(max(now_at, settle_until)) if settle_until is not None else now
                )
                state = LiveQueueState(
                    queue_id=f"cycle-{len(scenario_ids)}-{uuid.uuid4().hex[:8]}",
                    scenario_ids=scenario_ids,
                    catalog_snapshot_sha256=snapshot_sha256,
                    cycle_mode=True,
                    phase="cycle_reset",
                    phase_started_at=now,
                    expected_transition_at=reset_at,
                    reason=(
                        f"started; normal window held until {reset_at}"
                        if reset_at != now
                        else None
                    ),
                    started_at=now,
                    updated_at=now,
                )
            else:
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
            readiness = await asyncio.to_thread(self.readiness)
            if not readiness.ready:
                raise RuntimeError(f"live queue readiness failed: {', '.join(readiness.missing)}")
            if state.cycle_mode:
                # Operator resume of a paused cycle re-enters at the safe point:
                # restart the current scenario's cycle from trainer_reset. Timed
                # phases resume on their own (expected_transition_at persists), so
                # only the paused case reaches here.
                now_at = self.clock.now()
                now = self._format(now_at)
                # The paused run (a DIRTY one cleaned up by hand, typically)
                # gets the same settle a restart gives before the next normal
                # window. F63-R on 2026-10-26: cleanup ended 06:44:09, resume
                # at 06:49:28 started the baseline 15 minutes inside its tail.
                # Also every other recently active run: a pause that landed while
                # a restart was already holding cycle_reset has no current run
                # left, but its settle still stands (review R2).
                settle_candidates = [
                    value
                    for value in (
                        self._settle_not_before(state.current_run_id, now_at),
                        await asyncio.to_thread(self._recent_settle_not_before, now_at),
                    )
                    if value is not None
                ]
                settle_until = max(settle_candidates) if settle_candidates else None
                reset_at = (
                    self._format(max(now_at, settle_until)) if settle_until is not None else now
                )
                state = state.model_copy(
                    update={
                        "phase": "cycle_reset",
                        "current_scenario_id": None,
                        "current_run_id": None,
                        "cycle_started_at": None,
                        "cycle_buffer_started_at": None,
                        "cycle_golden_id": None,
                        "cycle_golden_sha256": None,
                        # Operator resume clears the restart budget: a fixed blocker
                        # shouldn't leave a stale count that pauses again immediately.
                        "cycle_restart_counts": {},
                        "phase_started_at": now,
                        "expected_transition_at": reset_at,
                        "reason": (
                            f"resumed; normal window held until {reset_at}"
                            if reset_at != now
                            else None
                        ),
                        "updated_at": now,
                    }
                )
                self._write(state)
                self.ensure_worker()
                return self.snapshot()
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
                resume_window = inter_scenario_gap()
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

    async def cancel(self, reason: str | None = None) -> LiveQueueState:
        """Close a paused queue without re-running its current scenario.

        ``resume`` always re-enters the paused scenario, so a queue whose only
        scenario was put on hold could never move on: ``start`` refuses while
        paused. Cancel ends it as ``completed`` (keeping the pause reason) so the
        operator can ``start`` the next candidate. It does not touch run
        evidence or a DIRTY coordinator; ``start`` still requires readiness.
        """
        async with self._lock:
            state = self.snapshot()
            if state.phase != "paused":
                raise RuntimeError("only a paused live queue can be cancelled")
            previous = state.reason or "no pause reason"
            state = await asyncio.to_thread(self._complete, state)
            note = f"cancelled by operator: {reason}" if reason else "cancelled by operator"
            state = state.model_copy(update={"reason": f"{note} (paused: {previous})"})
            self._write(state)
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
            self._event_loop = asyncio.get_running_loop()
            state = self.snapshot()
            if state.phase in {"idle", "paused", "completed"}:
                return state
            if self.runner.coordinator.snapshot().dirty_run is not None:
                return await asyncio.to_thread(
                    self._pause, state, "DIRTY coordinator state blocks the live queue"
                )
            if state.cycle_mode:
                return await self._tick_cycle(state)
            state = await asyncio.to_thread(self._append_promoted_if_available, state)
            if state.phase == "running":
                return await self._tick_running(state)
            if state.phase == "waiting_capture":
                return await asyncio.to_thread(self._tick_capture, state)
            # The clean-window tick re-probes operational readiness, which
            # shells out to the capture self-check (up to 120s). Both sync
            # ticks are file/subprocess work with no event-loop resources, so
            # they run on a thread — served inline they starved the coordinator
            # heartbeat until an active lease expired (#31, 2026-08-03 batch).
            return await asyncio.to_thread(self._tick_clean_window, state)

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
        """v2 running tick. The gate before the start (protection window, R6
        preflight probe, golden restore over ssh) and the evidence reads after
        it are blocking, so they run on worker threads; only runner.start runs
        on the loop."""
        if state.current_run_id is not None:
            current = self.runner.get_current()
            return await asyncio.to_thread(self._tick_running_watch, state, current)
        self.injection_reserved = True
        try:
            early, scenario_id, verdict = await asyncio.to_thread(
                self._prepare_v2_injection, state
            )
            if early is not None:
                return early
            # The clean-window / scenario_overlap gates enforce capture-window
            # separation, so a pass that captures nothing has nothing for them
            # to protect. The preflight gate above still ran, and it measures
            # cleanliness rather than assuming a gap.
            run = await self.runner.start(
                scenario_id=scenario_id,
                mode="run",
                skip_isolation_checks=not isolation_checks_enabled(),
            )
        except Exception as error:
            return await asyncio.to_thread(
                self._pause, state, f"start failed for {scenario_id}: {error}"
            )
        finally:
            self.injection_reserved = False
        return await asyncio.to_thread(
            self._enter_v2_running, state, scenario_id, run, verdict
        )

    def _prepare_v2_injection(
        self, state: LiveQueueState
    ) -> tuple[LiveQueueState | None, str | None, PreflightVerdict | None]:
        if state.next_index >= len(state.scenario_ids):
            return self._complete(state), None, None
        if self.runner.is_busy or self.runner.coordinator.snapshot().active_lease is not None:
            return state, None, None
        scenario_id = state.scenario_ids[state.next_index]
        # Daily normal-segment protection: never let an injection start
        # inside — or bleed into — the 00:00-02:00 KST clean window.
        deferral = self._protection_window_reason(self.clock.now())
        if deferral is not None:
            if state.reason == deferral:
                return state, None, None
            deferred = state.model_copy(
                update={"reason": deferral, "updated_at": self._format(self.clock.now())}
            )
            self._write(deferred)
            return deferred, None, None
        # R6 preflight gate: prove [t1-10m, t1] is clean before injecting.
        short_circuit, verdict = self._preflight_gate(state, scenario_id)
        if short_circuit is not None:
            return short_circuit, None, None
        try:
            self._restore_golden(scenario_id)
        except Exception as error:
            return (
                self._pause(state, f"golden restore failed for {scenario_id}: {error}"),
                None,
                None,
            )
        return None, scenario_id, verdict

    def _enter_v2_running(
        self, state: LiveQueueState, scenario_id: str, run, verdict: PreflightVerdict | None
    ) -> LiveQueueState:
        if verdict is not None:
            self._write_preflight(run.run_id, verdict)
        self._write_normal_segment_marker(run.run_id)
        self._write_batch_marker(run.run_id, state)
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

    def _tick_running_watch(self, state: LiveQueueState, current) -> LiveQueueState:
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
        if job is None and capture_enabled():
            return self._pause(state, f"capture was not scheduled: {state.current_run_id}")
        state = state.model_copy(
            update={"phase": "waiting_capture", "updated_at": self._format(self.clock.now())}
        )
        self._write(state)
        return state

    def _tick_capture(self, state: LiveQueueState) -> LiveQueueState:
        assert state.current_run_id is not None
        # With capture off there is no export to wait on or to prove: the run's
        # own evidence, already checked above, is the whole result of the pass.
        if capture_enabled():
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
        # The gap to the next scenario is measured from this run's t2. Read it
        # through _previous_t2, which falls back to the run's own state.json —
        # with capture off there is no job to carry it. Fail safe if neither is
        # readable: measuring from now is never shorter than measuring from t2.
        t2 = self._previous_t2(state.current_run_id) or self.clock.now()
        state = state.model_copy(
            update={
                "phase": "waiting_clean_window",
                "next_index": next_index,
                "completed_run_ids": completed,
                "first_gate_passed": first_gate,
                "current_scenario_id": None,
                "current_run_id": None,
                "clean_window_not_before": self._format(t2 + inter_scenario_gap()),
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
                now = self.clock.now()
                if blockers <= TRANSIENT_READINESS_CHECKS:
                    if self._probe_grace_since is None:
                        self._probe_grace_since = now
                    if now - self._probe_grace_since < READINESS_PROBE_GRACE:
                        return state
                return self._pause(
                    state, self._readiness_failure_reason(readiness, blockers)
                )
            return state
        self._probe_grace_since = None
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

    # ------------------------------------------------------------------
    # v3 continuous-cycle state machine (CYCLE_MODE only)
    # ------------------------------------------------------------------
    async def _tick_cycle(self, state: LiveQueueState) -> LiveQueueState:
        """Dispatch one cycle tick.

        Every phase does blocking work: ssh golden restore and trainer
        freeze/thaw (up to 900s), the R6 preflight probe and incident sweep
        against 119, run artifact reads. Done inline they froze the event loop
        that serves the API and runs the watchdog, which is how the runner went
        unresponsive for minutes after a cycle restart (2026-10-12, 10-22) and
        how a watchdog reclaimed a live run's lease (2026-10-26). So each phase
        runs on a worker thread. Two stay here and push their own blocking
        steps to threads: the buffer, whose injection start belongs to the
        runner's loop, and the normal window, whose decoy steps are async.
        """
        if state.phase == "cycle_normal":
            return await self._tick_cycle_normal(state)
        if state.phase == "cycle_buffer":
            return await self._tick_cycle_buffer(state)
        if state.phase == "running":
            current = self.runner.get_current()
            return await asyncio.to_thread(self._tick_cycle_running, state, current)
        handler = {
            "cycle_reset": self._tick_cycle_reset,
            "cycle_cooldown": self._tick_cycle_cooldown,
            "waiting_capture": self._tick_cycle_capture,
        }.get(state.phase)
        if handler is None:
            return await asyncio.to_thread(
                self._pause, state, f"unknown cycle phase: {state.phase}"
            )
        if state.phase != "cycle_reset":
            return await asyncio.to_thread(handler, state)
        # The golden restore rewrites AI state; no manual injection meanwhile.
        self.injection_reserved = True
        try:
            return await asyncio.to_thread(handler, state)
        finally:
            self.injection_reserved = False

    def _tick_cycle_reset(self, state: LiveQueueState) -> LiveQueueState:
        """Start a cycle: restore golden, then resume trainer for the 2h lead-in.

        Golden restore itself freezes the trainer (docker stop); we thaw straight
        after so the trainer learns on the clean normal-only window (spec §2.2 /
        spec-trainer-reset §5). Restore already retries once internally, so a
        raised failure is terminal -> pause (resume table: reset failure).
        """
        if state.next_index >= len(state.scenario_ids):
            return self._complete(state)
        if self.runner.is_busy or self.runner.coordinator.snapshot().active_lease is not None:
            return state
        # A restart or resume schedules this phase for after the previous
        # run's effect has ended and cooled down (see _settle_not_before); the
        # 2h normal window must not begin before then.
        if not self._transition_due(state):
            return state
        scenario_id = state.scenario_ids[state.next_index]
        try:
            golden = self._restore_golden(scenario_id)
        except Exception as error:
            return self._pause(state, f"golden reset failed for {scenario_id}: {error}")
        self._thaw_trainer()
        now = self.clock.now()
        try:
            decoy_seed, decoy_policy_sha256, decoys = self._plan_cycle_decoys(
                state, scenario_id, now
            )
        except Exception as error:
            return self._pause(state, f"decoy plan failed for {scenario_id}: {error}")
        state = state.model_copy(
            update={
                "phase": "cycle_normal",
                "current_scenario_id": scenario_id,
                "current_run_id": None,
                "cycle_started_at": self._format(now),
                "cycle_buffer_started_at": None,
                "phase_started_at": self._format(now),
                "expected_transition_at": self._format(now + CYCLE_NORMAL_DURATION),
                "cycle_golden_id": (golden or {}).get("id"),
                "cycle_golden_sha256": (golden or {}).get("sha256"),
                "cycle_decoy_seed": decoy_seed,
                "cycle_decoy_policy_sha256": decoy_policy_sha256,
                "cycle_decoys": decoys,
                # Keep why the previous attempt was restarted visible for the
                # whole normal window. Clearing it here is how the 2026-10-22
                # F35-P restart reason vanished before anyone read it.
                "reason": state.reason,
                "updated_at": self._format(now),
            }
        )
        self._write(state)
        # Begin periodic topology capture for the whole cycle (normal .. capture).
        self._start_cycle_topology(state)
        return state

    async def _tick_cycle_normal(self, state: LiveQueueState) -> LiveQueueState:
        """Hold the 2h lead-in. Baseline load is supplied out-of-band by the
        tb-runner systemd loadgen; the queue health-checks and runs the cycle's
        planned decoy deployments (U7). Any health failure restarts the whole
        cycle (resume table: normal).

        Runs on the loop because the decoy steps are async; each blocking step
        (health probe, restart, trainer freeze over ssh) goes to a thread."""
        health_error = await asyncio.to_thread(self._cycle_health_error)
        if health_error is not None:
            return await asyncio.to_thread(
                self._restart_cycle, state, f"normal-phase health failed: {health_error}"
            )
        state = await self._advance_decoys(state)
        if not self._transition_due(state):
            return state
        state = await self._settle_decoys_at_buffer(state)
        return await asyncio.to_thread(self._enter_cycle_buffer, state)

    def _enter_cycle_buffer(self, state: LiveQueueState) -> LiveQueueState:
        """Freeze the trainer at buffer start (=t1-10m) and open the 10m gate."""
        try:
            self._freeze_trainer()
        except Exception as error:
            return self._pause(state, f"trainer freeze failed at buffer: {error}")
        now = self.clock.now()
        state = state.model_copy(
            update={
                "phase": "cycle_buffer",
                "cycle_buffer_started_at": self._format(now),
                "phase_started_at": self._format(now),
                "expected_transition_at": self._format(now + CYCLE_BUFFER_DURATION),
                "preflight_attempts": 0,
                "preflight_retry_not_before": None,
                "reason": None,
                "updated_at": self._format(now),
            }
        )
        self._write(state)
        return state

    async def _tick_cycle_buffer(self, state: LiveQueueState) -> LiveQueueState:
        """Hold the 10m buffer, then run the R6 preflight gate over that same
        window and inject. A dirty buffer restarts the cycle rather than skipping
        (resume table: buffer -> cleanup-guaranteed cycle restart).

        The gate (preflight probe, incident sweep) runs on a worker thread and
        the bookkeeping after the start does too; runner.start stays on the
        loop because it creates the run's task there."""
        if self.runner.is_busy or self.runner.coordinator.snapshot().active_lease is not None:
            return state
        if not self._transition_due(state):
            return state
        scenario_id = state.scenario_ids[state.next_index]
        self.injection_reserved = True
        try:
            unsettled = await self._unsettled_decoy(state)
            if unsettled is not None:
                return await asyncio.to_thread(self._restart_cycle, state, unsettled)
            early, verdict = await asyncio.to_thread(self._cycle_buffer_gate, state, scenario_id)
            if early is not None:
                return early
            try:
                # The cycle time axis provides injection separation, so opt out of the
                # v2 clean-window / scenario_overlap eligibility gates (root fix for the
                # abandoned-run overlap that the exemption marker only patched).
                run = await self.runner.start(
                    scenario_id=scenario_id, mode="run", skip_isolation_checks=True
                )
            except Exception as error:
                return await asyncio.to_thread(
                    self._restart_cycle,
                    state,
                    f"injection start failed for {scenario_id}: {error}",
                )
        finally:
            self.injection_reserved = False
        return await asyncio.to_thread(self._enter_cycle_running, state, run, verdict)

    def _cycle_buffer_gate(
        self, state: LiveQueueState, scenario_id: str
    ) -> tuple[LiveQueueState | None, PreflightVerdict | None]:
        verdict = None
        # None = decoys off; a list (possibly empty) = decoys on for this cycle.
        decoys = state.cycle_decoys if state.cycle_decoy_seed is not None else None
        if self.preflight_probe is not None:
            now = self.clock.now().astimezone(timezone.utc)
            buffer_start = (
                parse_utc(state.cycle_buffer_started_at, field="cycle_buffer_started_at")
                if state.cycle_buffer_started_at
                else None
            )
            try:
                verdict = self._evaluate_preflight(
                    now, waited_sec=0, decoys=decoys, buffer_start=buffer_start
                )
                if not verdict.is_clean and self._sweep_lingering_incidents(verdict):
                    verdict = self._evaluate_preflight(
                        now, waited_sec=0, decoys=decoys, buffer_start=buffer_start
                    )
            except Exception as error:
                return (
                    self._pause(state, f"preflight probe failed for {scenario_id}: {error}"),
                    None,
                )
            if not verdict.is_clean:
                return (
                    self._restart_cycle(
                        state, f"preflight not clean at buffer end ({verdict.verdict})"
                    ),
                    None,
                )
        return None, verdict

    def _enter_cycle_running(
        self, state: LiveQueueState, run, verdict: PreflightVerdict | None
    ) -> LiveQueueState:
        if verdict is not None:
            self._write_preflight(run.run_id, verdict)
        # Decoys are final once the buffer opened; bind them to the run so the
        # capture forwards --decoys-json into meta.json.
        self._write_cycle_decoys(run.run_id, state)
        # Bind the topology bundle to this run so capture forwards --topology-bundle.
        self._write_topology_bundle_marker(run.run_id)
        now = self.clock.now()
        state = state.model_copy(
            update={
                "phase": "running",
                "current_run_id": run.run_id,
                "phase_started_at": self._format(now),
                "expected_transition_at": None,
                "reason": None,
                "updated_at": self._format(now),
            }
        )
        self._write(state)
        return state

    def _tick_cycle_running(self, state: LiveQueueState, current) -> LiveQueueState:
        """Watch the injection; on clean success enter cooldown, on a dirty run
        pause (cleanup not guaranteed), on any other failure restart the cycle.

        ``current`` is the runner's run snapshot, taken on the loop thread."""
        if current is not None and current.run_id == state.current_run_id:
            if current.status in {"running", "cleanup_running"}:
                return state
            if current.dirty:
                return self._pause(
                    state, self._with_run_reason(f"DIRTY run: {current.run_id}", current.run_id)
                )
            if current.status != "succeeded":
                return self._restart_cycle(
                    state,
                    self._with_run_reason(
                        f"scenario evidence failed: {current.run_id}", current.run_id
                    ),
                )
        elif self.runner.coordinator.snapshot().active_lease is not None:
            return state
        evidence_error = self._controller_evidence_error(state.current_run_id)
        if evidence_error is not None:
            evidence_error = self._with_run_reason(evidence_error, state.current_run_id)
            if "did not recover cleanly" in evidence_error:
                return self._pause(state, evidence_error)
            return self._restart_cycle(state, evidence_error)
        job = self._capture_job(state.current_run_id)
        if job is None:
            if capture_enabled():
                return self._pause(state, f"capture was not scheduled: {state.current_run_id}")
            # 스모크 패스 × 사이클: 캡처 잡이 없는 것이 정상이다. t2 는 캡처 계약이
            # 아니라 쿨다운 시계의 기점으로만 쓰이므로 주입 종료 직후(지금)로 둔다.
            t2 = self.clock.now()
        else:
            t2 = parse_utc(job.t2, field="t2")
        now = self.clock.now()
        # Write phases.json at cooldown ENTRY (t2), not cooldown end. All phase
        # boundaries are deterministic once t2 is known (cooldown.end = t2 +
        # CYCLE_COOLDOWN_DURATION), and the capture job fires at t2 + POST_WINDOW
        # (20m). When cooldown (30m) > POST_WINDOW, a cooldown-end write lands
        # after the capture invoker already built its args → --phases-json
        # missing, only --topology-bundle present → capture aborts. Writing here
        # guarantees phases.json exists well before any capture fire. (2026-07-25,
        # exposed on the production run — shortened smoke cooldown 5m<20m hid it.)
        if capture_enabled():
            self._write_cycle_phases(state)
        state = state.model_copy(
            update={
                "phase": "cycle_cooldown",
                "phase_started_at": self._format(now),
                "expected_transition_at": self._format(t2 + CYCLE_COOLDOWN_DURATION),
                "reason": None,
                "updated_at": self._format(now),
            }
        )
        self._write(state)
        return state

    def _tick_cycle_cooldown(self, state: LiveQueueState) -> LiveQueueState:
        """Hold the 30m recovery/settle window, then hand off to capture. Data
        already lives in the stores, so a runner restart just re-enters here and
        waits out the remaining time. phases.json was already written at cooldown
        entry (t2); re-write here is idempotent and covers a restart-mid-cooldown
        that skipped the entry write."""
        if not self._transition_due(state):
            return state
        if capture_enabled():
            self._write_cycle_phases(state)
        now = self.clock.now()
        state = state.model_copy(
            update={
                "phase": "waiting_capture",
                "phase_started_at": self._format(now),
                "expected_transition_at": None,
                "updated_at": self._format(now),
            }
        )
        self._write(state)
        return state

    def _tick_cycle_capture(self, state: LiveQueueState) -> LiveQueueState:
        """Wait for capture completion, thaw the trainer, then start the next
        cycle. Capture retries are owned by the scheduler; a terminal failure
        pauses (resume table: capture -> retry capture only)."""
        assert state.current_run_id is not None
        if capture_enabled():
            job = self._capture_job(state.current_run_id)
            if job is None:
                return self._pause(state, f"capture job disappeared: {state.current_run_id}")
            if job.status == "failed":
                return self._pause(state, f"capture failed: {state.current_run_id}: {job.failure}")
            if job.status != "completed":
                return state
            if not (
                self.runner.artifact_store.root / state.current_run_id / "capture-complete.json"
            ).is_file():
                return self._pause(state, f"capture completion evidence missing: {state.current_run_id}")
        # 스모크 패스는 캡처 산출물 없이 곧장 다음 사이클로 — 면제·해동은 동일하게 수행한다.
        # Exempt this completed run from the next cycle's clean-window overlap gate
        # (its captured residue is separated by the next reset + 2h normal lead-in).
        self._write_cycle_clean_window_excuse(
            state.current_run_id,
            state.current_scenario_id or state.scenario_ids[state.next_index],
            "cycle-complete",
        )
        # Topology capture ends with the cycle (trainer thaw point).
        self._stop_cycle_topology()
        self._thaw_trainer()
        completed = [*state.completed_run_ids, state.current_run_id]
        next_index = state.next_index + 1
        first_gate = state.first_gate_passed or state.next_index == 0
        now = self.clock.now()
        base = {
            "completed_run_ids": completed,
            "next_index": next_index,
            "first_gate_passed": first_gate,
        }
        if next_index >= len(state.scenario_ids):
            return self._complete(state.model_copy(update=base))
        state = state.model_copy(
            update={
                **base,
                "phase": "cycle_reset",
                "current_scenario_id": None,
                "current_run_id": None,
                "cycle_started_at": None,
                "cycle_buffer_started_at": None,
                "cycle_golden_id": None,
                "cycle_golden_sha256": None,
                "cycle_decoy_seed": None,
                "cycle_decoy_policy_sha256": None,
                "cycle_decoys": [],
                "phase_started_at": self._format(now),
                "expected_transition_at": self._format(now),
                "reason": None,
                "updated_at": self._format(now),
            }
        )
        self._write(state)
        return state

    def _write_cycle_clean_window_excuse(
        self, run_id: str, scenario_id: str, reason: str
    ) -> None:
        """Exempt a cycle run from the v2 clean-window overlap gate.

        The continuous cycle (reset + 2h normal + buffer) guarantees separation
        on its own time axis, so a prior cycle run left inside live_probes'
        30m overlap window must not block the next injection. Reuses the existing
        ``clean-window-excused.json`` marker honoured by
        ``LiveProbes._overlap_evidence``. Cycle mode only."""
        marker = self.runner.artifact_store.root / run_id / "clean-window-excused.json"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "scenario_id": scenario_id,
                    "reason": reason,
                    "excused_at": self._format(self.clock.now()),
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    def _restart_cycle(self, state: LiveQueueState, reason: str) -> LiveQueueState:
        """Restart the current scenario's cycle from trainer_reset. Two
        consecutive restarts are allowed; the third pauses (spec §2.2)."""
        scenario_id = state.current_scenario_id or state.scenario_ids[state.next_index]
        # The abandoned cycle's topology collector stops now; the fresh cycle_reset
        # starts a new one.
        self._stop_cycle_topology()
        # Read the run's effect window before the excuse marker below creates
        # its directory (a missing directory means nothing was applied).
        now = self.clock.now()
        settle_until = self._settle_not_before(state.current_run_id, now)
        # A run that actually started must not linger in the next cycle's overlap
        # window; excuse it before rewinding (only when injection reached a run).
        if state.current_run_id is not None:
            self._write_cycle_clean_window_excuse(
                state.current_run_id, scenario_id, "cycle-restart"
            )
        count = state.cycle_restart_counts.get(scenario_id, 0) + 1
        if count > MAX_CYCLE_RESTARTS:
            return self._pause(
                state, f"cycle restart budget exhausted for {scenario_id}: {reason}"
            )
        # The next 2h normal window is the case's clean baseline. Starting it
        # the moment an injected run gives up let that run's recovery echo
        # (incidents, error tails, restarts) into the baseline: F50-R's dump
        # was discarded for it on 2026-10-13. Hold cycle_reset until the run's
        # effect has ended plus the same cooldown a successful cycle gets.
        reset_at = max(now, settle_until) if settle_until is not None else now
        restart_reason = f"cycle restart {count}/{MAX_CYCLE_RESTARTS}: {reason}"
        if reset_at > now:
            restart_reason += f" (normal window held until {self._format(reset_at)})"
        logger.warning("live queue %s: %s", state.queue_id, restart_reason)
        state = state.model_copy(
            update={
                "phase": "cycle_reset",
                "current_scenario_id": None,
                "current_run_id": None,
                "cycle_started_at": None,
                "cycle_buffer_started_at": None,
                "cycle_golden_id": None,
                "cycle_golden_sha256": None,
                "cycle_decoy_seed": None,
                "cycle_decoy_policy_sha256": None,
                "cycle_decoys": [],
                "cycle_restart_counts": {**state.cycle_restart_counts, scenario_id: count},
                "phase_started_at": self._format(now),
                "expected_transition_at": self._format(reset_at),
                "reason": restart_reason,
                "updated_at": self._format(now),
            }
        )
        self._write(state)
        return state

    def _settle_not_before(self, run_id: str | None, now: datetime) -> datetime | None:
        """When a fresh normal window may start after ``run_id``.

        The run's effect end plus CYCLE_COOLDOWN_DURATION, the settle a
        successful cycle already gets after t2. None when the run never touched
        the testbed (no run directory, or one that proves no effect was
        applied). An effect whose end is unknown or still open counts as ending
        now: the caller only gets here once the run has stopped.
        """
        if run_id is None:
            return None
        run_dir = self.runner.artifact_store.root / run_id
        if not run_dir.is_dir():
            return None
        try:
            intervals = _run_intervals(run_dir)
        except Exception:
            intervals = [(now, now)]
        if not intervals:
            return None
        effect_end = min(max(end for _, end in intervals), now)
        return effect_end + CYCLE_COOLDOWN_DURATION

    def _recent_settle_not_before(self, now: datetime) -> datetime | None:
        """Latest settle over runs whose directory changed recently.

        A run directory is touched by every artifact the run writes, cleanup
        and manual cleanup included, so a run whose effect could still need
        settling has an mtime within the cooldown of now. Older ones are past
        it by construction and are not read."""
        root = self.runner.artifact_store.root
        horizon = now.timestamp() - CYCLE_COOLDOWN_DURATION.total_seconds() - 600
        latest: datetime | None = None
        try:
            entries = list(root.iterdir())
        except OSError:
            return None
        for run_dir in entries:
            try:
                if not run_dir.is_dir() or run_dir.stat().st_mtime < horizon:
                    continue
            except OSError:
                continue
            if not _is_run_directory(run_dir):
                continue
            settle = self._settle_not_before(run_dir.name, now)
            if settle is not None and (latest is None or settle > latest):
                latest = settle
        return latest

    def _with_run_reason(self, message: str, run_id: str | None) -> str:
        """Append the run's own failure reason (controller reason, which carries
        the profile's stderr) so the queue says why, not only which run."""
        if run_id is None:
            return message
        detail = self._controller_retry_reason(run_id)
        if not detail or detail in message:
            return message
        return f"{message}: {detail}"

    def _transition_due(self, state: LiveQueueState) -> bool:
        if state.expected_transition_at is None:
            return True
        now = self.clock.now().astimezone(timezone.utc)
        return now >= parse_utc(state.expected_transition_at, field="expected_transition_at")

    def _cycle_health_error(self) -> str | None:
        if self.cycle_health_probe is None:
            return None
        return self.cycle_health_probe()

    def _freeze_trainer(self) -> None:
        """Freeze the trainer at buffer start. No-op unless golden reset is on."""
        if not self.golden_reset_enabled:
            return
        self._run_restore(["--freeze"], timeout=60)

    @staticmethod
    def _parse_golden_result(
        result: "subprocess.CompletedProcess[str] | None",
    ) -> dict[str, str | None] | None:
        """Best-effort golden identity from the restore script's JSON stdout.

        restore-golden-state.sh emits golden_dir and golden_sha256 (the sha256 of
        the dump it restored, spec §2.2). phases.json trainer_reset carries both
        and the capture copies them into the recording meta. A restore script
        that predates the digest, or a value that is not a 64-char lowercase hex
        digest, leaves golden_sha256 null rather than failing the cycle.
        """
        if result is None:
            return None
        try:
            payload = json.loads((result.stdout or "").strip() or "{}")
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        golden_dir = payload.get("golden_dir")
        sha256 = payload.get("golden_sha256")
        return {
            "id": Path(golden_dir).name if isinstance(golden_dir, str) and golden_dir else None,
            "sha256": sha256 if isinstance(sha256, str) and _SHA256_HEX.fullmatch(sha256) else None,
        }

    def _write_cycle_phases(self, state: LiveQueueState) -> None:
        """Write <run-id>/phases.json (spec §2.2 phases[]) so the capture invoker
        forwards it as --phases-json. Boundaries are the actual recorded cycle
        times; injection/cooldown derive from the capture job's t1/t2."""
        run_id = state.current_run_id
        if run_id is None or state.cycle_started_at is None:
            return
        job = self._capture_job(run_id)
        if job is None:
            return
        t1, t2 = job.t1, job.t2
        cycle_start = state.cycle_started_at
        buffer_start = state.cycle_buffer_started_at or cycle_start
        cooldown_end = self._format(
            parse_utc(t2, field="t2") + CYCLE_COOLDOWN_DURATION
        )
        document = {
            "schema_version": "2.0",
            "timeline": "continuous",
            "run_id": run_id,
            "scenario_id": state.current_scenario_id,
            "capture_start": cycle_start,
            "capture_end": cooldown_end,
            "phases": [
                {
                    "phase": "trainer_reset",
                    "at": cycle_start,
                    "golden_id": state.cycle_golden_id,
                    "golden_sha256": state.cycle_golden_sha256,
                },
                {"phase": "normal", "start": cycle_start, "end": buffer_start},
                {"phase": "buffer", "start": buffer_start, "end": t1},
                {"phase": "injection", "start": t1, "end": t2},
                {"phase": "cooldown", "start": t2, "end": cooldown_end},
            ],
        }
        path = self.runner.artifact_store.root / run_id / "phases.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(document, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(raw, path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(raw)

    def _cycle_topology_dir(self, state: LiveQueueState) -> Path:
        return (
            self.runner.artifact_store.root
            / "cycle-topology"
            / f"{state.queue_id}-{state.next_index}"
        )

    def _start_cycle_topology(self, state: LiveQueueState) -> None:
        """Start the per-cycle topology snapshot collector. Fail-open: a factory
        or start error must never block the cycle."""
        if self.topology_collector_factory is None:
            return
        self._stop_cycle_topology()
        try:
            bundle_dir = self._cycle_topology_dir(state)
            # A restart reuses the same bundle dir (queue_id-next_index); wipe any
            # prior instance's files first so the fresh manifest exactly describes
            # the directory contents (capture ingest contract: manifest covers all).
            if bundle_dir.exists():
                shutil.rmtree(bundle_dir)
            collector = self.topology_collector_factory(bundle_dir)
            self._topology_collector = collector

            def start() -> None:
                # Runs on the loop, possibly after this tick moved on: undo the
                # binding on failure so no empty bundle is tied to a run.
                try:
                    collector.start()
                except Exception:
                    if self._topology_collector is collector:
                        self._topology_collector = None

            self._call_on_event_loop(start)
        except Exception:
            self._topology_collector = None

    def _call_on_event_loop(self, fn: Callable[[], object]) -> None:
        """Run a loop-bound call (create_task) from wherever the tick runs.

        On the loop thread it runs now. From a tick's worker thread it is queued
        on the loop ahead of the tick's own completion, so it has run by the
        time the awaiting tick resumes."""
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        loop = self._event_loop
        if on_loop or loop is None or loop.is_closed():
            fn()
        else:
            loop.call_soon_threadsafe(fn)

    def _stop_cycle_topology(self) -> None:
        collector = self._topology_collector
        if collector is None:
            return
        with suppress(Exception):
            collector.stop()
        self._topology_collector = None

    def _write_topology_bundle_marker(self, run_id: str) -> None:
        """Point the capture invoker at this cycle's topology bundle (fail-open,
        same pattern as preflight/phases markers). Cycle mode only."""
        collector = self._topology_collector
        if collector is None:
            return
        with suppress(Exception):
            collector.set_run_id(run_id)
        bundle_dir = getattr(collector, "bundle_dir", None)
        if bundle_dir is None:
            return
        path = self.runner.artifact_store.root / run_id / "topology-bundle.path"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{bundle_dir}\n", encoding="utf-8")

    # ------------------------------------------------------------------
    # Decoy deployments inside the cycle (U7, cycle mode only)
    # ------------------------------------------------------------------
    def _decoy_policy(self, *, reload: bool = False):
        """(policy, sha256) of the decoy registry, or None when decoys are off."""
        if self.decoy_policy_path is None:
            return None
        cached = getattr(self, "_decoy_policy_cache", None)
        if cached is None or reload:
            cached = load_decoy_policy(self.decoy_policy_path)
            self._decoy_policy_cache = cached
        return cached

    def _decoy_executor_for(self, policy) -> object:
        if self._decoy_executor is None:
            self._decoy_executor = KubectlDecoyExecutor(policy)
        return self._decoy_executor

    def _plan_cycle_decoys(
        self, state: LiveQueueState, scenario_id: str, cycle_start: datetime
    ) -> tuple[int | None, str | None, list[DecoyRecord]]:
        """Seeded schedule for this cycle. The seed comes from values the
        recording keeps (queue, scenario, cycle start), so the same inputs give
        the same decoys; it is also written next to the run."""
        loaded = self._decoy_policy(reload=True)
        if loaded is None:
            return None, None, []
        policy, policy_sha256 = loaded
        cycle_start_text = self._format(cycle_start)
        seed = cycle_seed(self.decoy_seed_base, state.queue_id, scenario_id, cycle_start_text)
        registry = json.loads(self.runner.scenario_metadata_path.read_text(encoding="utf-8"))
        scenario = (registry.get("scenarios") or {}).get(scenario_id) or {}
        records = build_schedule(
            policy,
            seed=seed,
            scenario_id=scenario_id,
            scenario=scenario,
            cycle_start=parse_utc(cycle_start_text, field="cycle_start"),
            buffer_start=parse_utc(cycle_start_text, field="cycle_start") + CYCLE_NORMAL_DURATION,
        )
        return seed, policy_sha256, records

    async def _advance_decoys(self, state: LiveQueueState) -> LiveQueueState:
        """One step of the decoy schedule per tick, never blocking on a rollout.

        A rolling decoy is polled (one ``get``) until healthy or past the rollout
        timeout, then undone. Only one decoy is in flight at a time. A decoy whose
        time passed while the runner was away runs late only within the policy's
        late limit and never after the near-fault window closes; otherwise it is
        recorded as skipped, not silently dropped."""
        loaded = self._decoy_policy()
        if loaded is None or not state.cycle_decoys:
            return state
        policy, _ = loaded
        executor = self._decoy_executor_for(policy)
        now = self.clock.now().astimezone(timezone.utc)
        records = list(state.cycle_decoys)
        changed = False
        for index, record in enumerate(records):
            if record.status != "rolling":
                continue
            records[index] = await self._poll_decoy(executor, record, now, policy.rollout_timeout_sec)
            changed = changed or records[index] != record
            if records[index].status == "rolling":
                return self._store_decoys(state, records) if changed else state
        cycle_start = parse_utc(state.cycle_started_at, field="cycle_started_at")
        last_start = (
            cycle_start
            + CYCLE_NORMAL_DURATION
            - timedelta(seconds=policy.windows["near_fault"].end_before_buffer_sec)
        )
        for index, record in enumerate(records):
            if record.status != "planned":
                continue
            planned_at = parse_utc(record.planned_at, field="decoy planned_at")
            if now < planned_at:
                break
            if now > planned_at + timedelta(seconds=policy.late_limit_sec) or now > last_start:
                records[index] = record.model_copy(
                    update={"status": "skipped", "detail": "runner missed the planned time"}
                )
                changed = True
                continue
            records[index] = await self._start_decoy(executor, record, now)
            changed = True
            break
        return self._store_decoys(state, records) if changed else state

    async def _start_decoy(self, executor, record: DecoyRecord, now: datetime) -> DecoyRecord:
        started = record.model_copy(update={"started_at": self._format(now)})
        try:
            await asyncio.to_thread(executor.apply, started)
        except Exception as error:
            return started.model_copy(
                update={
                    "status": "failed",
                    "finished_at": self._format(self.clock.now()),
                    "detail": f"{type(error).__name__}: {error}",
                }
            )
        if started.kind in ROLLOUT_KINDS:
            return started.model_copy(update={"status": "rolling"})
        return started.model_copy(
            update={"status": "applied", "finished_at": self._format(self.clock.now())}
        )

    async def _poll_decoy(
        self, executor, record: DecoyRecord, now: datetime, timeout_sec: int, *, final: bool = False
    ) -> DecoyRecord:
        try:
            done = await asyncio.to_thread(executor.rollout_done, record)
        except Exception as error:
            done = False
            detail = f"{type(error).__name__}: {error}"
        else:
            detail = None
        if done:
            return record.model_copy(
                update={"status": "applied", "finished_at": self._format(now)}
            )
        started = parse_utc(record.started_at or self._format(now), field="decoy started_at")
        if not final and now < started + timedelta(seconds=timeout_sec):
            return record
        reason = "rollout not healthy before the buffer" if final else "rollout timed out"
        try:
            await asyncio.to_thread(executor.undo, record)
        except Exception as error:
            return record.model_copy(
                update={
                    "status": "failed",
                    "finished_at": self._format(now),
                    "detail": f"{reason}; undo failed: {type(error).__name__}: {error}",
                }
            )
        return record.model_copy(
            update={
                "status": "rolled_back",
                "finished_at": self._format(now),
                "detail": reason if detail is None else f"{reason}; {detail}",
            }
        )

    async def _settle_decoys_at_buffer(self, state: LiveQueueState) -> LiveQueueState:
        """Nothing of the schedule may reach into the buffer: a rollout still in
        flight gets one last check and is undone if unhealthy, and decoys that
        never ran are recorded as skipped."""
        loaded = self._decoy_policy()
        if loaded is None or not state.cycle_decoys:
            return state
        policy, _ = loaded
        executor = self._decoy_executor_for(policy)
        now = self.clock.now().astimezone(timezone.utc)
        records = []
        for record in state.cycle_decoys:
            if record.status == "rolling":
                record = await self._poll_decoy(
                    executor, record, now, policy.rollout_timeout_sec, final=True
                )
            elif record.status == "planned":
                record = record.model_copy(
                    update={"status": "skipped", "detail": "buffer reached before the planned time ran"}
                )
            records.append(record)
        return self._store_decoys(state, records)

    def _store_decoys(self, state: LiveQueueState, records: list[DecoyRecord]) -> LiveQueueState:
        state = state.model_copy(
            update={"cycle_decoys": records, "updated_at": self._format(self.clock.now())}
        )
        self._write(state)
        return state

    def _decoy_alarm_filter(
        self, decoys: list[DecoyRecord], now: datetime, buffer_start: datetime | None
    ):
        loaded = self._decoy_policy()
        grace = loaded[0].alarm_excuse_grace_sec if loaded is not None else 0

        def apply(alerts):
            return partition_decoy_alarms(
                alerts, decoys, grace_sec=grace, now=now, buffer_start=buffer_start
            )

        return apply

    async def _unsettled_decoy(self, state: LiveQueueState) -> str | None:
        """A decoy undone at the buffer starts a rollback rollout that nothing
        waited for. If it is still rolling when the buffer ends, the clean
        window the gate vouches for is not clean: restart the cycle."""
        loaded = self._decoy_policy()
        if loaded is None:
            return None
        executor = self._decoy_executor_for(loaded[0])
        for record in state.cycle_decoys:
            if record.status not in {"rolled_back", "failed"} or record.deployment is None:
                continue
            if record.kind not in ROLLOUT_KINDS:
                continue
            try:
                done = await asyncio.to_thread(executor.rollout_done, record)
            except Exception as error:
                return f"decoy {record.id} rollout state unreadable at buffer end: {error}"
            if not done:
                return f"decoy {record.id} rollback still rolling at buffer end"
        return None

    def _write_cycle_decoys(self, run_id: str, state: LiveQueueState) -> None:
        """<run-id>/decoys.json, forwarded by capture as --decoys-json into
        meta.json. Written only when the feature is on, including when the
        cycle drew zero decoys (that is a recording without decoys, not a
        recording from a runner that could not make them)."""
        if state.cycle_decoy_seed is None:
            return
        document = decoy_document(
            scenario_id=state.current_scenario_id or "",
            seed=state.cycle_decoy_seed,
            policy_sha256=state.cycle_decoy_policy_sha256,
            cycle_start=state.cycle_started_at,
            records=state.cycle_decoys,
        )
        path = self.runner.artifact_store.root / run_id / "decoys.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(document, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(raw, path)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(raw)

    def _cycle_contract(self, selected_ids: tuple[str, ...] | None = None) -> tuple[list[str], str]:
        """Freeze the v3 cycle's scenario list and its digest.

        Sourced from the same approved registry the v2 queue freezes, so both
        modes cover the same catalogue and inherit its manifest validation.
        CYCLE_SCENARIOS narrows it to a subset for a targeted recapture; ids
        outside the approved list are refused rather than run, since a typo
        there would otherwise produce a capture nobody asked for.
        """
        approved, digest = self._queue_contract()
        chosen = self.cycle_scenario_ids if selected_ids is None else selected_ids
        if not chosen:
            return approved, digest
        selected = list(chosen)
        if len(selected) != len(set(selected)):
            raise RuntimeError("cycle queue scenario ids must be unique")
        unknown = [item for item in selected if item not in set(approved)]
        if unknown:
            raise RuntimeError(f"cycle scenarios are not in the approved list: {sorted(unknown)}")
        for scenario_id in selected:
            load_scenario_metadata(self.runner.scenario_metadata_path, scenario_id)
        # Bind the digest to both the approved catalogue and the chosen subset:
        # either changing means a different run.
        encoded = json.dumps(selected, separators=(",", ":")).encode()
        return selected, hashlib.sha256(digest.encode() + encoded).hexdigest()

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
            return self._probe_error_wait(state, scenario_id, error, now), None
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
                return self._probe_error_wait(state, scenario_id, error, now), None
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

    def _probe_error_wait(
        self, state: LiveQueueState, scenario_id: str, error: Exception, now: datetime
    ) -> LiveQueueState:
        """A probe that raised is transport, not a verdict — so wait, don't stop.

        Sibling of the readiness-gate hole fixed the same day: a dirty verdict
        already gets 6 x 5m of rechecks, but an exception ended the whole batch on
        the first occurrence. 119 answers both gates, and it resets connections
        under load (2026-08-04: ConnectionResetError here, again minutes later),
        so the batch died on a blip that cleared by itself. Spend the same budget;
        a probe still broken after it is an environment failure worth pausing on.
        """
        attempts = state.preflight_attempts + 1
        detail = f"{type(error).__name__}: {error}"
        if attempts >= PREFLIGHT_MAX_ATTEMPTS:
            return self._pause(
                state, f"preflight probe failed for {scenario_id}: {detail}"
            )
        waiting = state.model_copy(
            update={
                "preflight_attempts": attempts,
                "preflight_retry_not_before": self._format(now + PREFLIGHT_RECHECK_INTERVAL),
                "reason": (
                    f"preflight probe failed for {scenario_id} ({detail}); "
                    f"recheck {attempts}/{PREFLIGHT_MAX_ATTEMPTS}"
                ),
                "updated_at": self._format(now),
            }
        )
        self._write(waiting)
        return waiting

    def _evaluate_preflight(
        self,
        now: datetime,
        waited_sec: int,
        *,
        decoys: list[DecoyRecord] | None = None,
        buffer_start: datetime | None = None,
    ) -> PreflightVerdict:
        """R6 gate. With decoys in the cycle, a firing alarm that one of them
        explains (its target, inside its time span, decoy finished healthy) is
        not residue: it leaves ``active_alarms`` and is listed in the verdict's
        ``excused_alarms`` instead. Every other alarm still fails the gate."""
        assert self.preflight_probe is not None
        set_filter = getattr(self.preflight_probe, "set_alarm_filter", None)
        decoy_filter = (
            None if decoys is None else self._decoy_alarm_filter(decoys, now, buffer_start)
        )
        if callable(set_filter):
            set_filter(decoy_filter)
        try:
            observations = self.preflight_probe.collect(now=now)
        finally:
            if callable(set_filter):
                set_filter(None)
        excused = None
        if decoy_filter is not None and callable(set_filter):
            excused = list(getattr(self.preflight_probe, "last_excused_alarms", None) or [])
        checks = build_preflight_checks(observations)
        window = (self._format(now - PREFLIGHT_WINDOW_LEAD), self._format(now))
        return evaluate_preflight(
            window=window,
            checks=checks,
            now=now,
            waited_sec=waited_sec,
            ai_judge=self.preflight_ai_judge,
            excused_alarms=excused,
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

    def _run_restore(
        self, args: list[str], *, timeout: int = 900
    ) -> "subprocess.CompletedProcess[str] | None":
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
        try:
            return self._restore_runner(
                ["ssh", "-i", str(ssh_key), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                 "-o", "ConnectTimeout=10", target, remote],
                check=True, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.CalledProcessError as error:
            # capture_output swallows the script's diagnostics; without this the
            # pause reason only says "exit status 1" and the failure cannot be
            # diagnosed after the fact (observed 2026-07-21, twice).
            tail = "\n".join(str(error.stderr or error.stdout or "").splitlines()[-8:])
            raise RuntimeError(
                f"restore script exit {error.returncode}: {tail.strip() or 'no output captured'}"
            ) from error

    def _restore_golden(self, scenario_id: str) -> dict[str, str | None] | None:
        """Freeze trainer + reset AI DB state + restart observer to a clean golden.

        In v2 this runs just before injection; in v3 it runs at cycle start.
        Returns the restored golden's identity (id/sha256) when available, else
        None. No-op unless golden reset is enabled."""
        if not self.golden_reset_enabled:
            return None
        args = ["--golden-root", str(self.golden_store_root), "--for-time", "now"]
        try:
            result = self._run_restore(args)
        except Exception:
            # Transient failures right at capture-end transitions (observer
            # connections still draining) have twice resolved on immediate
            # retry; give it one settle-and-retry before pausing the queue.
            time.sleep(self.restore_retry_delay_sec)
            result = self._run_restore(args)
        return self._parse_golden_result(result)

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

        Returns None unconditionally when the protection is switched off — see
        _protection_window_enabled.
        """
        if not protection_window_enabled():
            return None
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

    def _write_batch_marker(self, run_id: str, state: LiveQueueState) -> None:
        """Record which batch this run belongs to, inside the run's own artifacts.

        Without this the batch is only reconstructible from the queue state, which
        holds one batch at a time — once the next batch starts, the previous run's
        membership is gone. Comparing batches then means guessing from timestamps,
        and on 2026-08-09 three different guesses were all wrong: "the last N runs"
        pulled in runs from outside the batch, a one-hour gap detector missed the
        ~13 minute inter-batch pause, and fixed-size windows skewed because a batch
        retries some scenarios (F20-R ran three times inside one batch).

        Fail-open: a marker that cannot be written must never stop a run.
        """
        if state.queue_id is None:
            return
        try:
            path = self.runner.artifact_store.root / run_id / "batch.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "queue_id": state.queue_id,
                        "index": state.next_index,
                        "started_at": state.started_at,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            return

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
                return inter_scenario_gap()
            result = json.loads(
                (self.runner.artifact_store.root / run_id / "result.json").read_text(encoding="utf-8")
            )
            if result.get("dirty") is not False:
                return inter_scenario_gap()
            if result.get("cleanup", {}).get("status") != "succeeded":
                return inter_scenario_gap()
            changes = result.get("level_changes") or []
            starts = [c.get("applied_at") for c in changes if c.get("applied_at")]
            ends = [c.get("effect_ended_at") for c in changes if c.get("effect_ended_at")]
            if not starts or len(ends) != len(starts):
                return inter_scenario_gap()
            span = parse_utc(max(ends), field="effect_ended_at") - parse_utc(
                min(starts), field="applied_at"
            )
            if span > RETRY_SHORT_INJECTION:
                return inter_scenario_gap()
        except Exception:
            return inter_scenario_gap()
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
        if state.phase in ACTIVE_PHASES and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while self.snapshot().phase in ACTIVE_PHASES:
            try:
                await self.tick()
            except Exception as error:
                async with self._lock:
                    await asyncio.to_thread(
                        self._pause,
                        self.snapshot(),
                        f"queue worker failed: {type(error).__name__}: {error}",
                    )
            await asyncio.sleep(self.poll_interval_sec)

    async def stop_worker(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def _pause(self, state: LiveQueueState, reason: str) -> LiveQueueState:
        logger.warning("live queue %s paused: %s", state.queue_id, reason)
        self._stop_cycle_topology()
        self._thaw_trainer()
        state = state.model_copy(
            update={"phase": "paused", "reason": reason, "updated_at": self._format(self.clock.now())}
        )
        self._write(state)
        return state

    def _complete(self, state: LiveQueueState) -> LiveQueueState:
        self._stop_cycle_topology()
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
        cycle_mode = os.environ.get("CYCLE_MODE", "").lower() in {"1", "true", "yes", "on"}
        topology_collector_factory = None
        if cycle_mode and os.environ.get("TOPOLOGY_SNAPSHOT", "on").lower() not in {
            "off", "0", "disabled"
        }:
            from app.topology_collector import (
                DEFAULT_INTERVAL_SEC,
                TopologyCollector,
                build_query_fetch,
            )

            interval = int(os.environ.get("TOPOLOGY_SNAPSHOT_INTERVAL_SEC", DEFAULT_INTERVAL_SEC))
            query_url = os.environ.get("LUCIDA_QUERY_URL", "http://192.168.230.119:18080")

            def topology_collector_factory(bundle_dir: Path) -> TopologyCollector:
                return TopologyCollector(
                    bundle_dir=bundle_dir,
                    fetch=build_query_fetch(query_url=query_url),
                    clock=SystemClock(),
                    interval_sec=interval,
                    query_url=query_url,
                )

        # Decoy deployments (U7) are opt-in: DECOY_DEPLOYS=1 on a cycle-mode
        # runner. The policy is the testbed-services registry file shipped with
        # the scenario contracts.
        decoy_policy_path = None
        if cycle_mode and os.environ.get("DECOY_DEPLOYS", "").lower() in {"1", "true", "yes", "on"}:
            decoy_policy_path = Path(
                os.environ.get(
                    "DECOY_POLICY_PATH",
                    "/opt/lucida/scenario-contracts/registry/decoy-deployments.json",
                )
            )
        # CYCLE_SCENARIOS narrows a cycle run to a subset for targeted
        # single/subset recaptures (e.g. F01-G recalibration) without a code
        # change. Comma-separated ids; unset covers the whole approved registry.
        cycle_scenarios_env = os.environ.get("CYCLE_SCENARIOS", "").strip()
        cycle_scenario_ids = tuple(
            item.strip() for item in cycle_scenarios_env.split(",") if item.strip()
        )
        _queue = LiveScenarioQueue(
            runner,
            state_path,
            scenario_registry_path=registry_path,
            manifest_root=manifest_root,
            preflight_probe=preflight_probe,
            functional_readiness_enabled=True,
            golden_reset_enabled=golden_reset_enabled(
                os.environ.get("GOLDEN_RESET_ENABLED", "").lower()
                in {"1", "true", "yes", "on"}
            ),
            cycle_mode=cycle_mode,
            cycle_scenario_ids=cycle_scenario_ids,
            topology_collector_factory=topology_collector_factory,
            decoy_policy_path=decoy_policy_path,
            decoy_seed_base=os.environ.get("DECOY_SEED_BASE", "testbed-v4-decoy"),
        )
    return _queue
