"""Deterministic orchestration around the pure adaptive state machine.

The runtime owns no subprocess, network client, filesystem store, or timer.  A
runner supplies read-only eligibility/observation dependencies, a fenced and
idempotent profile applier, and an explicit clock.  The complete session is a
strict Pydantic document so it can be atomically persisted between calls.
"""
from __future__ import annotations

import inspect
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from app.adaptive import (
    ConditionSet,
    ControllerAction,
    ControllerPhase,
    ControllerState,
    Observation,
    ObservedValue,
    advance,
    finalize_cleanup,
    start,
)
from app.controller import ControllerSpec
from app.observations import (
    CLOCK_SKEW_TOLERANCE_SEC,
    ObservationBlocked,
    ObservationPoller,
)


def stderr_detail(error: BaseException, *, limit: int = 160) -> str:
    """Reason suffix carrying a failed subprocess's stderr tail, or "" if none.

    A reason of only the exception class name cannot be diagnosed from the run
    artifacts — the operator sees "CalledProcessError" and has to reproduce the
    call by hand to learn anything (F09-P run 2333e298, F01-R run 0104bd01).
    Whitespace is collapsed so the tail stays one line inside a reason string.
    """
    tail = " ".join(str(getattr(error, "stderr", "") or "").split())[-limit:]
    return f":{tail}" if tail else ""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SessionStatus(StrEnum):
    PENDING = "pending"
    BLOCKED = "blocked"
    ACTIVE = "active"
    RECOVERING = "recovering"
    CLEAN = "clean"
    DIRTY = "dirty"


class EligibilityRequest(StrictModel):
    run_id: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    checks: list[str] = Field(min_length=1)
    clean_window_sec: int = Field(ge=1800)
    requested_at: datetime


class EligibilityEvidence(StrictModel):
    """Read-only proof that the pre-scenario clean window is safe to consume."""

    checked_at: datetime
    source: str = Field(min_length=1)
    quality: str = Field(pattern="^good$")
    check_results: dict[str, bool]
    clean_window_start: datetime
    clean_window_end: datetime
    overlapping_run_ids: list[str] = Field(default_factory=list)
    baseline_active: bool


class ApplyRequest(StrictModel):
    run_id: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    fencing_token: int = Field(gt=0)
    profile_id: str = Field(min_length=1)
    level_index: int = Field(ge=0)
    level_id: str = Field(min_length=1)
    parameters: dict[str, JsonValue]
    idempotency_key: str = Field(min_length=1)
    requested_at: datetime


class ApplyResult(StrictModel):
    run_id: str = Field(min_length=1)
    fencing_token: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1)
    applied_at: datetime


class CleanupRequest(StrictModel):
    run_id: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    fencing_token: int = Field(gt=0)
    profile_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    requested_at: datetime


class CleanupResult(StrictModel):
    run_id: str = Field(min_length=1)
    fencing_token: int = Field(gt=0)
    idempotency_key: str = Field(min_length=1)
    succeeded: bool
    effect_ended_at: datetime | None = None
    reason: str | None = None

    @model_validator(mode="after")
    def require_end_for_success(self) -> "CleanupResult":
        if self.succeeded and self.effect_ended_at is None:
            raise ValueError("successful cleanup requires effect_ended_at")
        return self


class LevelChange(StrictModel):
    level_index: int = Field(ge=0)
    level_id: str = Field(min_length=1)
    parameters: dict[str, JsonValue]
    idempotency_key: str = Field(min_length=1)
    requested_at: datetime
    applied_at: datetime
    effect_ended_at: datetime | None = None


class CleanupEvidence(StrictModel):
    requested_at: datetime
    idempotency_key: str
    succeeded: bool
    effect_ended_at: datetime | None = None
    reason: str | None = None


class RecoveryEvidence(StrictModel):
    status: str = Field(pattern="^(pending|succeeded|failed)$")
    started_at: datetime
    consecutive_ticks: int = Field(default=0, ge=0)
    verified_at: datetime | None = None
    reason: str | None = None


class ControllerSession(StrictModel):
    schema_version: int = Field(default=1, ge=1, le=1)
    run_id: str = Field(min_length=1)
    scenario_id: str = Field(min_length=1)
    fencing_token: int = Field(gt=0)
    profile_id: str = Field(min_length=1)
    approved_profile_id: str | None = None
    spec: ControllerSpec
    status: SessionStatus = SessionStatus.PENDING
    created_at: datetime
    eligibility: EligibilityEvidence | None = None
    blocked_reasons: list[str] = Field(default_factory=list)
    controller_state: ControllerState | None = None
    level_changes: list[LevelChange] = Field(default_factory=list)
    cleanup: CleanupEvidence | None = None
    recovery: RecoveryEvidence | None = None
    terminal_at: datetime | None = None
    # v3 continuous cycle opts out of the clean-window/scenario_overlap isolation
    # gates (the cycle time axis provides separation). Persisted so restart keeps
    # the same decision. Default False = v2 behaviour unchanged.
    skip_isolation_checks: bool = False

    @model_validator(mode="after")
    def validate_evaluation_profile(self) -> "ControllerSession":
        if self.spec.baseline.clean_window_sec < 1800:
            raise ValueError("adaptive runtime requires at least a 30-minute clean window")
        if self.spec.adaptive.mode.value == "evaluation":
            if self.approved_profile_id is None or self.profile_id != self.approved_profile_id:
                raise ValueError("evaluation requires the exactly approved fixed profile")
            if len(self.spec.adaptive.levels) != 1:
                raise ValueError("evaluation requires exactly one fixed level")
        return self

    @property
    def t1(self) -> datetime | None:
        return min((change.applied_at for change in self.level_changes), default=None)

    @property
    def t2(self) -> datetime | None:
        ends = [
            change.effect_ended_at
            for change in self.level_changes
            if change.effect_ended_at is not None
        ]
        return max(ends, default=None)

    def trusted_evidence(self) -> dict[str, Any]:
        """Return controller evidence for capture scheduling/result construction."""
        outcome = None
        if self.controller_state is not None:
            outcome = self.controller_state.phase.value
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "mode": self.spec.adaptive.mode.value,
            "outcome": outcome,
            "dirty": self.status == SessionStatus.DIRTY,
            "profile": {
                "kind": "fixed" if self.spec.adaptive.mode.value == "evaluation" else "adaptive_ladder",
                "id": self.profile_id,
            },
            "approved_profile_id": self.approved_profile_id,
            "t1": _format_utc(self.t1),
            "t2": _format_utc(self.t2),
            "cleanup": {
                "status": "succeeded" if self.cleanup and self.cleanup.succeeded else "failed"
            },
            "recovery": {
                "status": self.recovery.status if self.recovery else "not_verified"
            },
            "baseline": self.eligibility.model_dump(mode="json") if self.eligibility else None,
            "level_changes": [item.model_dump(mode="json") for item in self.level_changes],
            "fencing_token": self.fencing_token,
        }


class Clock(Protocol):
    def now(self) -> datetime: ...


class ReadOnlyEligibilityProbe(Protocol):
    def inspect(self, request: EligibilityRequest) -> EligibilityEvidence: ...


class ProfileApplier(Protocol):
    """Implementations must fence and deduplicate every request by its keys."""

    def apply(self, request: ApplyRequest) -> ApplyResult: ...

    def cleanup(self, request: CleanupRequest) -> CleanupResult: ...


class AdaptiveRuntime:
    """Explicitly-ticked controller runtime with no implicit waiting or I/O."""

    def __init__(
        self,
        session: ControllerSession,
        *,
        clock: Clock,
        eligibility_probe: ReadOnlyEligibilityProbe,
        poller: ObservationPoller,
        applier: ProfileApplier,
    ) -> None:
        self.session = session
        self.clock = clock
        self.eligibility_probe = eligibility_probe
        self.poller = poller
        self.applier = applier
        # Diagnostic snapshot of the most recent tick's polled signals. The
        # driver persists it (ticks.jsonl); it is not part of the session
        # schema and is intentionally reset on every tick.
        self.last_tick: dict[str, Any] | None = None

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        scenario_id: str,
        fencing_token: int,
        profile_id: str,
        approved_profile_id: str | None,
        spec: ControllerSpec,
        clock: Clock,
        eligibility_probe: ReadOnlyEligibilityProbe,
        poller: ObservationPoller,
        applier: ProfileApplier,
        skip_isolation_checks: bool = False,
    ) -> "AdaptiveRuntime":
        session = ControllerSession(
            run_id=run_id,
            scenario_id=scenario_id,
            fencing_token=fencing_token,
            profile_id=profile_id,
            approved_profile_id=approved_profile_id,
            spec=spec,
            created_at=_aware(clock.now()),
            skip_isolation_checks=skip_isolation_checks,
        )
        return cls(
            session,
            clock=clock,
            eligibility_probe=eligibility_probe,
            poller=poller,
            applier=applier,
        )

    @classmethod
    def restore(
        cls,
        raw: str,
        *,
        clock: Clock,
        eligibility_probe: ReadOnlyEligibilityProbe,
        poller: ObservationPoller,
        applier: ProfileApplier,
    ) -> "AdaptiveRuntime":
        return cls(
            ControllerSession.model_validate_json(raw),
            clock=clock,
            eligibility_probe=eligibility_probe,
            poller=poller,
            applier=applier,
        )

    async def begin(self) -> ControllerSession:
        if self.session.status != SessionStatus.PENDING:
            return self.session
        now = self._now()
        checks = list(dict.fromkeys(
            [*self.session.spec.preflight_checks, *self.session.spec.baseline.checks]
        ))
        request = EligibilityRequest(
            run_id=self.session.run_id,
            scenario_id=self.session.scenario_id,
            checks=checks,
            clean_window_sec=self.session.spec.baseline.clean_window_sec,
            requested_at=now,
        )
        evidence = await _maybe_await(self.eligibility_probe.inspect(request))
        reasons = _eligibility_reasons(
            request, evidence, skip_isolation_checks=self.session.skip_isolation_checks
        )
        self.session = self.session.model_copy(
            update={
                "eligibility": evidence,
                "blocked_reasons": reasons,
                "status": SessionStatus.BLOCKED if reasons else SessionStatus.ACTIVE,
            },
            deep=True,
        )
        if reasons:
            return self.session
        state = start(self.session.spec.adaptive)
        self.session = self.session.model_copy(update={"controller_state": state}, deep=True)
        await self._apply_current_level()
        return self.session

    async def tick(self) -> ControllerSession:
        self.last_tick = None
        if self.session.status == SessionStatus.RECOVERING:
            await self._tick_recovery()
            return self.session
        if self.session.status != SessionStatus.ACTIVE:
            return self.session
        state = self.session.controller_state
        if state is None or state.level_index is None:
            raise RuntimeError("active session has no controller level")
        now = self._now()
        t1 = self.session.t1
        if t1 is None:
            raise RuntimeError("active session has no applied level")
        if (now - t1).total_seconds() >= self.session.spec.max_injection_duration_sec:
            self.session = self.session.model_copy(
                update={
                    "controller_state": state.model_copy(
                        update={
                            "phase": ControllerPhase.ABORTED,
                            "action": ControllerAction.ABORT,
                            "reason": "maximum_injection_duration",
                        }
                    ),
                    "terminal_at": now,
                },
                deep=True,
            )
            await self._cleanup_terminal()
            return self.session

        signals = await self._poll_signals(now)
        level_started = self.session.level_changes[-1].applied_at
        elapsed = max(0, int((now - level_started).total_seconds()))
        self.last_tick = _tick_snapshot(now, elapsed, signals)
        next_state = advance(
            self.session.spec.adaptive,
            state,
            Observation(elapsed_sec=elapsed, signals=signals),
        )
        if (
            next_state.action == ControllerAction.ESCALATE
            and not self._decision_observations_usable(signals)
        ):
            next_state = next_state.model_copy(
                update={
                    "phase": ControllerPhase.ABORTED,
                    "action": ControllerAction.ABORT,
                    "reason": "decision_observation_unavailable",
                }
            )
        self.session = self.session.model_copy(update={"controller_state": next_state}, deep=True)
        if next_state.action == ControllerAction.ESCALATE:
            if await self._cleanup_for_level_transition():
                await self._apply_current_level()
        elif next_state.terminal:
            self.session = self.session.model_copy(update={"terminal_at": now}, deep=True)
            await self._cleanup_terminal()
        return self.session

    async def _cleanup_for_level_transition(self) -> bool:
        state = self.session.controller_state
        if state is None or state.level_index is None or not self.session.level_changes:
            raise RuntimeError("level transition has no previous effect")
        previous = self.session.level_changes[-1]
        request = CleanupRequest(
            run_id=self.session.run_id,
            scenario_id=self.session.scenario_id,
            fencing_token=self.session.fencing_token,
            profile_id=self.session.profile_id,
            idempotency_key=(
                f"transition-cleanup:{self.session.run_id}:{self.session.fencing_token}:"
                f"{previous.level_index}:{state.level_index}"
            ),
            requested_at=self._now(),
        )
        try:
            action = getattr(self.applier, "transition_cleanup", self.applier.cleanup)
            result = await _maybe_await(action(request))
            _validate_fenced_result(request, result)
        except Exception as error:
            result = CleanupResult(
                run_id=request.run_id,
                fencing_token=request.fencing_token,
                idempotency_key=request.idempotency_key,
                succeeded=False,
                reason=f"transition_cleanup_exception:{type(error).__name__}",
            )
        if result.succeeded and result.effect_ended_at is not None:
            self._close_latest_effect(_aware(result.effect_ended_at))
            return True
        self.session = self.session.model_copy(
            update={
                "controller_state": state.model_copy(
                    update={
                        "phase": ControllerPhase.ABORTED,
                        "action": ControllerAction.ABORT,
                        "reason": "level_transition_cleanup_failed",
                    }
                ),
                "terminal_at": self._now(),
            },
            deep=True,
        )
        await self._cleanup_terminal()
        return False

    def _decision_observations_usable(self, signals: dict[str, ObservedValue]) -> bool:
        condition_sets = [
            self.session.spec.adaptive.success,
            self.session.spec.adaptive.escalate,
            self.session.spec.adaptive.abort,
            self.session.spec.adaptive.must_rule_out,
        ]
        required = {
            condition.signal
            for condition_set in condition_sets
            if condition_set is not None
            for condition in condition_set.conditions
        }
        return all(signal in signals and signals[signal].usable for signal in required)

    async def _poll_signals(self, now: datetime) -> dict[str, ObservedValue]:
        signals: dict[str, ObservedValue] = {}
        for query in self.session.spec.adapter_queries:
            observed: ObservedValue | None = None
            for _attempt in range(2):
                try:
                    values = await self.poller.poll(
                        [{"query_id": query.query_id, "parameters": query.parameters}]
                    )
                    observed = values[query.query_id]
                except ObservationBlocked as error:
                    observed = error.blocked[query.query_id]
                except Exception as error:
                    observed = ObservedValue(
                        value=None,
                        observed_at=now,
                        source=f"poller:{query.query_id}:{type(error).__name__}",
                        freshness="fresh",
                        quality="error",
                    )
                if observed.observed_at is not None:
                    age = (now - _aware(observed.observed_at)).total_seconds()
                    if not -CLOCK_SKEW_TOLERANCE_SEC <= age <= query.freshness_sec:
                        observed = observed.model_copy(update={"freshness": "stale"})
                if observed.usable:
                    break
            assert observed is not None
            signals[query.id] = observed
        return signals

    async def _apply_current_level(self) -> None:
        state = self.session.controller_state
        assert state is not None and state.level_index is not None and state.level_id is not None
        level = self.session.spec.adaptive.levels[state.level_index]
        key = (
            f"profile:{self.session.run_id}:{self.session.fencing_token}:"
            f"{state.level_index}:{_parameters_digest(level.parameters)}"
        )
        existing = next(
            (item for item in self.session.level_changes if item.idempotency_key == key), None
        )
        if existing is not None:
            return
        requested_at = self._now()
        request = ApplyRequest(
            run_id=self.session.run_id,
            scenario_id=self.session.scenario_id,
            fencing_token=self.session.fencing_token,
            profile_id=self.session.profile_id,
            level_index=state.level_index,
            level_id=state.level_id,
            parameters=level.parameters,
            idempotency_key=key,
            requested_at=requested_at,
        )
        try:
            result = await _maybe_await(self.applier.apply(request))
            _validate_fenced_result(request, result)
            applied_at = _aware(result.applied_at)
            if applied_at < requested_at:
                raise RuntimeError("profile result applied_at precedes its request")
            if (
                self.session.level_changes
                and self.session.level_changes[-1].effect_ended_at is None
            ):
                self._close_latest_effect(applied_at)
            change = LevelChange(
                level_index=request.level_index,
                level_id=request.level_id,
                parameters=request.parameters,
                idempotency_key=key,
                requested_at=requested_at,
                applied_at=applied_at,
            )
            self.session = self.session.model_copy(
                update={"level_changes": [*self.session.level_changes, change]}, deep=True
            )
        except Exception as error:
            detail = stderr_detail(error)
            failed = state.model_copy(
                update={
                    "phase": ControllerPhase.ABORTED,
                    "action": ControllerAction.ABORT,
                    "reason": f"profile_apply_failed:{type(error).__name__}{detail}",
                }
            )
            self.session = self.session.model_copy(
                update={"controller_state": failed, "terminal_at": self._now()}, deep=True
            )
            await self._cleanup_terminal()

    async def _cleanup_terminal(self) -> None:
        if self.session.cleanup is not None:
            return
        now = self._now()
        key = f"cleanup:{self.session.run_id}:{self.session.fencing_token}"
        request = CleanupRequest(
            run_id=self.session.run_id,
            scenario_id=self.session.scenario_id,
            fencing_token=self.session.fencing_token,
            profile_id=self.session.profile_id,
            idempotency_key=key,
            requested_at=now,
        )
        try:
            result = await _maybe_await(self.applier.cleanup(request))
            _validate_fenced_result(request, result)
        except Exception as error:
            result = CleanupResult(
                run_id=request.run_id,
                fencing_token=request.fencing_token,
                idempotency_key=request.idempotency_key,
                succeeded=False,
                reason=f"cleanup_exception:{type(error).__name__}{stderr_detail(error)}",
            )
        if result.effect_ended_at is not None:
            self._close_latest_effect(_aware(result.effect_ended_at))
        state = self.session.controller_state
        assert state is not None and state.terminal
        final_state = finalize_cleanup(state, cleanup_succeeded=result.succeeded)
        cleanup = CleanupEvidence(
            requested_at=now,
            idempotency_key=key,
            succeeded=result.succeeded,
            effect_ended_at=result.effect_ended_at,
            reason=result.reason,
        )
        self.session = self.session.model_copy(
            update={
                "controller_state": final_state,
                "cleanup": cleanup,
                "status": SessionStatus.RECOVERING if result.succeeded else SessionStatus.DIRTY,
                "recovery": RecoveryEvidence(
                    status="pending" if result.succeeded else "failed",
                    started_at=result.effect_ended_at or now,
                    verified_at=None if result.succeeded else now,
                    reason=None if result.succeeded else "cleanup_failed",
                ),
            },
            deep=True,
        )
        if result.succeeded:
            await self._tick_recovery()

    async def _tick_recovery(self) -> None:
        recovery = self.session.recovery
        if recovery is None or recovery.status != "pending":
            return
        now = self._now()
        elapsed = (now - recovery.started_at).total_seconds()
        signals = await self._poll_signals(now)
        self.last_tick = _tick_snapshot(now, int(elapsed), signals)
        matched = _condition_set_matches(self.session.spec.recovery.conditions, signals)
        streak = recovery.consecutive_ticks + 1 if matched is True else 0
        required = self.session.spec.recovery.conditions.consecutive_ticks
        if streak >= required:
            self.session = self.session.model_copy(
                update={
                    "status": SessionStatus.CLEAN,
                    "recovery": recovery.model_copy(
                        update={
                            "status": "succeeded",
                            "consecutive_ticks": streak,
                            "verified_at": now,
                        }
                    ),
                },
                deep=True,
            )
            return
        if elapsed >= self.session.spec.recovery.timeout_sec:
            state = self.session.controller_state
            assert state is not None
            self.session = self.session.model_copy(
                update={
                    "status": SessionStatus.DIRTY,
                    "controller_state": state.model_copy(
                        update={
                            "phase": ControllerPhase.DIRTY,
                            "action": ControllerAction.MARK_DIRTY,
                            "reason": "recovery_timeout",
                            "dirty": True,
                        }
                    ),
                    "recovery": recovery.model_copy(
                        update={
                            "status": "failed",
                            "consecutive_ticks": streak,
                            "verified_at": now,
                            "reason": "recovery_timeout",
                        }
                    ),
                },
                deep=True,
            )
            return
        self.session = self.session.model_copy(
            update={"recovery": recovery.model_copy(update={"consecutive_ticks": streak})},
            deep=True,
        )

    def _close_latest_effect(self, ended_at: datetime) -> None:
        if not self.session.level_changes:
            return
        changes = list(self.session.level_changes)
        current = changes[-1]
        if current.effect_ended_at is None or ended_at > current.effect_ended_at:
            if ended_at < current.applied_at:
                raise ValueError("effect end cannot precede level application")
            changes[-1] = current.model_copy(update={"effect_ended_at": ended_at})
            self.session = self.session.model_copy(update={"level_changes": changes}, deep=True)

    def _now(self) -> datetime:
        return _aware(self.clock.now())


def _eligibility_reasons(
    request: EligibilityRequest,
    evidence: EligibilityEvidence,
    *,
    skip_isolation_checks: bool = False,
) -> list[str]:
    requested_at = _aware(request.requested_at)
    checked_at = _aware(evidence.checked_at)
    window_start = _aware(evidence.clean_window_start)
    window_end = _aware(evidence.clean_window_end)
    # The clean-window and scenario_overlap gates are v2-queue isolation
    # guarantees. The v3 continuous cycle (reset + 2h normal + buffer) already
    # separates injections on its own time axis, so it opts out of both — the
    # previous cycle's run legitimately sits inside the 30m overlap window.
    reasons = [
        f"check_failed:{check}"
        for check in request.checks
        if evidence.check_results.get(check) is not True
        and not (skip_isolation_checks and check == "clean-window")
    ]
    window = (window_end - window_start).total_seconds()
    if not skip_isolation_checks:
        if window < request.clean_window_sec:
            reasons.append("clean_window_too_short")
        if window_end > checked_at:
            reasons.append("clean_window_after_check")
    if checked_at < requested_at:
        reasons.append("eligibility_before_request")
    if evidence.overlapping_run_ids and not skip_isolation_checks:
        reasons.append("scenario_overlap")
    if not evidence.baseline_active:
        reasons.append("baseline_inactive")
    return reasons


def _validate_fenced_result(request: ApplyRequest | CleanupRequest, result: Any) -> None:
    if (
        result.run_id != request.run_id
        or result.fencing_token != request.fencing_token
        or result.idempotency_key != request.idempotency_key
    ):
        raise RuntimeError("profile result rejected by fencing/idempotency contract")


def _condition_set_matches(
    condition_set: ConditionSet, signals: dict[str, ObservedValue]
) -> bool | None:
    results: list[bool | None] = []
    for condition in condition_set.conditions:
        observed = signals.get(condition.signal)
        if observed is None or not observed.usable:
            results.append(None)
            continue
        actual = observed.value
        expected = condition.value
        if condition.operator in {"gt", "gte", "lt", "lte"}:
            if isinstance(actual, bool) or not isinstance(actual, (int, float)):
                results.append(False)
            elif condition.operator == "gt":
                results.append(actual > expected)
            elif condition.operator == "gte":
                results.append(actual >= expected)
            elif condition.operator == "lt":
                results.append(actual < expected)
            else:
                results.append(actual <= expected)
        elif condition.operator == "eq":
            results.append(actual == expected)
        else:
            results.append(actual != expected)
    known = [result for result in results if result is not None]
    if condition_set.match == "all":
        if any(result is False for result in known):
            return False
        return True if len(known) == len(results) else None
    if any(result is True for result in known):
        return True
    return False if len(known) == len(results) else None


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _tick_snapshot(
    now: datetime,
    elapsed_sec: int,
    signals: Mapping[str, ObservedValue],
) -> dict[str, Any]:
    return {
        "at": _format_utc(_aware(now)),
        "elapsed_sec": elapsed_sec,
        "signals": {
            name: {**value.model_dump(mode="json"), "usable": value.usable}
            for name, value in signals.items()
        },
    }


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("runtime timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _format_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _aware(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parameters_digest(parameters: dict[str, Any]) -> str:
    from json import dumps

    payload = dumps(parameters, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256(payload.encode()).hexdigest()[:16]
