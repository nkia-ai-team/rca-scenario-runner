"""Side-effect-free adaptive scenario state machine.

The module owns no clock and performs no I/O. Callers provide level-relative
elapsed time and observation envelopes; equal inputs always produce equal
state transitions.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


Scalar = bool | int | float | str


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdaptiveMode(StrEnum):
    CALIBRATION = "calibration"
    EVALUATION = "evaluation"


class ControllerPhase(StrEnum):
    READY = "ready"
    SETTLING = "settling"
    EVALUATING = "evaluating"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"
    DIRTY = "dirty"


class ControllerAction(StrEnum):
    START = "start"
    WAIT = "wait"
    SUCCEED = "succeed"
    ESCALATE = "escalate"
    FAIL = "fail"
    ABORT = "abort"
    MARK_CLEAN = "mark_clean"
    MARK_DIRTY = "mark_dirty"


class Condition(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    signal: str = Field(min_length=1)
    operator: Literal["gt", "gte", "lt", "lte", "eq", "ne"]
    value: Scalar

    @model_validator(mode="after")
    def validate_comparison(self) -> "Condition":
        if self.operator in {"gt", "gte", "lt", "lte"}:
            if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
                raise ValueError(f"condition {self.id}: ordered comparison requires a number")
            if not isfinite(float(self.value)):
                raise ValueError(f"condition {self.id}: comparison value must be finite")
        return self


class ConditionSet(StrictModel):
    match: Literal["all", "any"] = "all"
    conditions: list[Condition] = Field(min_length=1)
    consecutive_ticks: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "ConditionSet":
        ids = [condition.id for condition in self.conditions]
        if len(ids) != len(set(ids)):
            raise ValueError("condition ids must be unique within a condition set")
        return self


class AdaptiveLevel(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    min_hold_sec: int = Field(default=0, ge=0)
    settle_sec: int = Field(ge=0)
    timeout_sec: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_window(self) -> "AdaptiveLevel":
        if self.timeout_sec < max(self.settle_sec, self.min_hold_sec):
            raise ValueError(
                f"level {self.id}: timeout_sec must be >= settle_sec and min_hold_sec"
            )
        return self


class AdaptiveSpec(StrictModel):
    mode: AdaptiveMode
    levels: list[AdaptiveLevel] = Field(min_length=1)
    success: ConditionSet
    escalate: ConditionSet | None = None
    abort: ConditionSet | None = None
    must_rule_out: ConditionSet = Field(
        description="Alternative-cause evidence; a true match aborts the run"
    )

    @model_validator(mode="after")
    def validate_mode(self) -> "AdaptiveSpec":
        ids = [level.id for level in self.levels]
        if len(ids) != len(set(ids)):
            raise ValueError("adaptive level ids must be unique")
        if self.mode == AdaptiveMode.EVALUATION and len(self.levels) != 1:
            raise ValueError("evaluation mode requires exactly one level")
        if (
            self.mode == AdaptiveMode.CALIBRATION
            and len(self.levels) > 1
            and self.escalate is None
        ):
            raise ValueError("multi-level calibration requires escalate conditions")
        return self


class ObservedValue(StrictModel):
    value: Scalar | None = None
    observed_at: datetime | None = None
    source: str = Field(min_length=1)
    freshness: Literal["fresh", "stale"]
    quality: Literal["good", "error"]
    # Failure detail (exit code + stderr tail) for quality="error" signals;
    # absent on healthy observations.
    error: str | None = None

    @property
    def usable(self) -> bool:
        return self.freshness == "fresh" and self.quality == "good" and self.value is not None


class Observation(StrictModel):
    """Signals observed at a monotonically increasing level-relative time."""

    elapsed_sec: int = Field(ge=0)
    signals: dict[str, ObservedValue] = Field(default_factory=dict)


class ControllerState(StrictModel):
    phase: ControllerPhase = ControllerPhase.READY
    level_index: int | None = None
    level_id: str | None = None
    last_elapsed_sec: int = 0
    streaks: dict[str, int] = Field(default_factory=dict)
    action: ControllerAction | None = None
    reason: str | None = None
    dirty: bool = False

    @property
    def terminal(self) -> bool:
        return self.phase in {
            ControllerPhase.SUCCEEDED,
            ControllerPhase.FAILED,
            ControllerPhase.ABORTED,
            ControllerPhase.DIRTY,
        }


def start(spec: AdaptiveSpec) -> ControllerState:
    """Start calibration at level zero or the sole evaluation level."""
    level = spec.levels[0]
    return ControllerState(
        phase=ControllerPhase.SETTLING,
        level_index=0,
        level_id=level.id,
        action=ControllerAction.START,
        reason="level_started",
    )


def advance(
    spec: AdaptiveSpec,
    state: ControllerState,
    observation: Observation,
) -> ControllerState:
    """Evaluate one observation envelope and return a new state snapshot."""
    if state.phase == ControllerPhase.READY:
        raise ValueError("controller must be started before it can advance")
    if state.terminal:
        return state.model_copy(deep=True)
    if state.level_index is None or state.level_id is None:
        raise ValueError("active controller state must identify a level")
    if observation.elapsed_sec < state.last_elapsed_sec:
        raise ValueError("observation elapsed_sec must be monotonic within a level")

    level = spec.levels[state.level_index]
    if level.id != state.level_id:
        raise ValueError("controller state does not match adaptive specification")

    raw_abort = _matches(spec.abort, observation.signals)
    raw_ruleout = _matches(spec.must_rule_out, observation.signals)
    raw_success = _matches(spec.success, observation.signals)
    raw_escalate = _matches(spec.escalate, observation.signals)
    streaks = _updated_streaks(
        state.streaks,
        abort=raw_abort,
        must_rule_out=raw_ruleout,
        success=raw_success,
        escalate=raw_escalate,
    )
    current = state.model_copy(
        update={"last_elapsed_sec": observation.elapsed_sec, "streaks": streaks},
        deep=True,
    )

    if _confirmed(spec.abort, streaks, "abort"):
        return _transition(
            current, ControllerPhase.ABORTED, ControllerAction.ABORT, "abort_condition"
        )
    if _confirmed(spec.must_rule_out, streaks, "must_rule_out"):
        return _transition(
            current,
            ControllerPhase.ABORTED,
            ControllerAction.ABORT,
            "must_rule_out_detected",
        )

    safety_unknown = _has_unusable_signal(spec.abort, observation.signals) or raw_ruleout is None
    safety_pending = raw_abort is True or raw_ruleout is True

    if observation.elapsed_sec < level.settle_sec:
        return _transition(
            current, ControllerPhase.SETTLING, ControllerAction.WAIT, "settling"
        )
    if observation.elapsed_sec < level.min_hold_sec:
        return _transition(
            current, ControllerPhase.EVALUATING, ControllerAction.WAIT, "min_hold"
        )

    if not safety_unknown and not safety_pending and _confirmed(spec.success, streaks, "success"):
        return _transition(
            current, ControllerPhase.SUCCEEDED, ControllerAction.SUCCEED, "success"
        )

    if (
        not safety_unknown
        and not safety_pending
        and _confirmed(spec.escalate, streaks, "escalate")
    ):
        return _escalate_or_fail(spec, current, "escalate_condition")

    if observation.elapsed_sec >= level.timeout_sec:
        if safety_unknown:
            return _transition(
                current,
                ControllerPhase.ABORTED,
                ControllerAction.ABORT,
                "safety_observation_unavailable",
            )
        if safety_pending:
            return _transition(
                current,
                ControllerPhase.ABORTED,
                ControllerAction.ABORT,
                "safety_condition_unresolved",
            )
        if spec.mode == AdaptiveMode.CALIBRATION:
            return _escalate_or_fail(spec, current, "level_timeout")
        return _transition(
            current,
            ControllerPhase.FAILED,
            ControllerAction.FAIL,
            "evaluation_level_timeout",
        )

    reason = "safety_observation_pending" if safety_unknown or safety_pending else "conditions_pending"
    return _transition(current, ControllerPhase.EVALUATING, ControllerAction.WAIT, reason)


def finalize_cleanup(state: ControllerState, *, cleanup_succeeded: bool) -> ControllerState:
    """Attach a cleanup outcome without performing cleanup."""
    if not state.terminal:
        raise ValueError("cleanup can only finalize a terminal controller state")
    if not cleanup_succeeded:
        return state.model_copy(
            update={
                "phase": ControllerPhase.DIRTY,
                "action": ControllerAction.MARK_DIRTY,
                "reason": "cleanup_failed",
                "dirty": True,
            },
            deep=True,
        )
    if state.phase == ControllerPhase.DIRTY:
        raise ValueError("a dirty outcome requires explicit recovery validation")
    return state.model_copy(
        update={"action": ControllerAction.MARK_CLEAN, "dirty": False}, deep=True
    )


def _matches(
    condition_set: ConditionSet | None,
    signals: dict[str, ObservedValue],
) -> bool | None:
    if condition_set is None:
        return None
    results = [_condition_result(condition, signals) for condition in condition_set.conditions]
    known = [result for result in results if result is not None]
    if condition_set.match == "all":
        if any(result is False for result in known):
            return False
        return True if len(known) == len(results) else None
    if any(result is True for result in known):
        return True
    return False if len(known) == len(results) else None


def _condition_result(
    condition: Condition,
    signals: dict[str, ObservedValue],
) -> bool | None:
    observed = signals.get(condition.signal)
    if observed is None or not observed.usable:
        return None
    actual: Any = observed.value
    expected = condition.value
    if condition.operator in {"gt", "gte", "lt", "lte"}:
        if isinstance(actual, bool) or not isinstance(actual, (int, float)):
            return False
        if not isfinite(float(actual)):
            return False
        if condition.operator == "gt":
            return actual > expected
        if condition.operator == "gte":
            return actual >= expected
        if condition.operator == "lt":
            return actual < expected
        return actual <= expected
    if condition.operator == "eq":
        return actual == expected
    return actual != expected


def _updated_streaks(streaks: dict[str, int], **results: bool | None) -> dict[str, int]:
    updated = dict(streaks)
    for name, result in results.items():
        updated[name] = updated.get(name, 0) + 1 if result is True else 0
    return updated


def _confirmed(
    condition_set: ConditionSet | None,
    streaks: dict[str, int],
    name: str,
) -> bool:
    return condition_set is not None and streaks.get(name, 0) >= condition_set.consecutive_ticks


def _has_unusable_signal(
    condition_set: ConditionSet | None,
    signals: dict[str, ObservedValue],
) -> bool:
    if condition_set is None:
        return False
    return any(
        condition.signal not in signals or not signals[condition.signal].usable
        for condition in condition_set.conditions
    )


def _escalate_or_fail(
    spec: AdaptiveSpec,
    state: ControllerState,
    reason: str,
) -> ControllerState:
    if spec.mode == AdaptiveMode.EVALUATION:
        return _transition(
            state,
            ControllerPhase.FAILED,
            ControllerAction.FAIL,
            "evaluation_level_insufficient",
        )
    assert state.level_index is not None
    next_index = state.level_index + 1
    if next_index >= len(spec.levels):
        return _transition(
            state,
            ControllerPhase.FAILED,
            ControllerAction.FAIL,
            "calibration_levels_exhausted",
        )
    next_level = spec.levels[next_index]
    return state.model_copy(
        update={
            "phase": ControllerPhase.SETTLING,
            "level_index": next_index,
            "level_id": next_level.id,
            "last_elapsed_sec": 0,
            "streaks": {},
            "action": ControllerAction.ESCALATE,
            "reason": reason,
        },
        deep=True,
    )


def _transition(
    state: ControllerState,
    phase: ControllerPhase,
    action: ControllerAction,
    reason: str,
) -> ControllerState:
    return state.model_copy(
        update={"phase": phase, "action": action, "reason": reason}, deep=True
    )
