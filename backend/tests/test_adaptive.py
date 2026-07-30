import pytest
from pydantic import ValidationError

from app.adaptive import (
    AdaptiveLevel,
    AdaptiveMode,
    AdaptiveSpec,
    ConditionSet,
    ControllerAction,
    ControllerPhase,
    ControllerState,
    Observation,
    advance,
    finalize_cleanup,
    start,
)


def _value(value=0, *, freshness="fresh", quality="good", source="test") -> dict:
    return {"value": value, "source": source, "freshness": freshness, "quality": quality}


def _spec(*, mode: AdaptiveMode = AdaptiveMode.CALIBRATION) -> AdaptiveSpec:
    levels = [
        {
            "id": "low",
            "parameters": {"rps": 20},
            "min_hold_sec": 10,
            "settle_sec": 5,
            "timeout_sec": 30,
        },
        {
            "id": "high",
            "parameters": {"rps": 40},
            "min_hold_sec": 15,
            "settle_sec": 10,
            "timeout_sec": 45,
        },
    ]
    if mode == AdaptiveMode.EVALUATION:
        levels = [levels[1]]
    return AdaptiveSpec.model_validate(
        {
            "mode": mode,
            "levels": levels,
            "success": {
                "conditions": [
                    {
                        "id": "impact",
                        "signal": "error_rate",
                        "operator": "gte",
                        "value": 0.05,
                    }
                ],
                "consecutive_ticks": 2,
            },
            "escalate": {
                "conditions": [
                    {
                        "id": "too-weak",
                        "signal": "error_rate",
                        "operator": "lt",
                        "value": 0.05,
                    }
                ],
                "consecutive_ticks": 2,
            },
            "abort": {
                "match": "any",
                "conditions": [
                    {
                        "id": "unsafe",
                        "signal": "health",
                        "operator": "eq",
                        "value": False,
                    }
                ],
                "consecutive_ticks": 2,
            },
            "must_rule_out": {
                "match": "any",
                "conditions": [
                    {
                        "id": "alternative-db-lock",
                        "signal": "db_lock",
                        "operator": "eq",
                        "value": True,
                    }
                ],
            },
        }
    )


def _safe_signals(*, error_rate=0.0, health=True, db_lock=False, update_interval_sec=0) -> dict:
    return {
        "error_rate": {**_value(error_rate), "update_interval_sec": update_interval_sec},
        "health": _value(health),
        "db_lock": _value(db_lock),
    }


def test_consecutive_ticks_counts_samples_not_reads_of_the_same_sample() -> None:
    """A 60s metric cannot confirm a two-tick gate inside one minute.

    The tick interval (15s) is shorter than every prometheus metric's update
    interval (60s, measured 2026-07-30), so counting ticks let a gate reach
    ``consecutive_ticks`` by reading one sample repeatedly. 24 of the 44 live
    scenarios were confirming on a single sample.
    """
    spec = _spec(mode=AdaptiveMode.EVALUATION)
    slow = start(spec)
    slow = advance(spec, slow, Observation(elapsed_sec=15, signals=_safe_signals(
        error_rate=0.2, update_interval_sec=60)))
    assert slow.streaks["success"] == 1
    slow = advance(spec, slow, Observation(elapsed_sec=30, signals=_safe_signals(
        error_rate=0.2, update_interval_sec=60)))
    assert slow.streaks["success"] == 1, "re-reading the same 60s sample is not new evidence"
    assert slow.phase is not ControllerPhase.SUCCEEDED
    # One update interval later the source has genuinely produced a new value.
    slow = advance(spec, slow, Observation(elapsed_sec=75, signals=_safe_signals(
        error_rate=0.2, update_interval_sec=60)))
    assert slow.phase is ControllerPhase.SUCCEEDED

    # A signal the adapter re-queries every tick is independent every tick.
    fast = start(spec)
    fast = advance(spec, fast, Observation(elapsed_sec=15, signals=_safe_signals(error_rate=0.2)))
    fast = advance(spec, fast, Observation(elapsed_sec=30, signals=_safe_signals(error_rate=0.2)))
    assert fast.phase is ControllerPhase.SUCCEEDED


def test_any_gate_renews_at_the_pace_of_the_condition_carrying_it() -> None:
    """An idle slow condition must not slow down a live fast one.

    F12-H aborts on ``any`` of {entry unreachable (polled every tick), pod
    network errors (60s)}. Pacing the set by its slowest member would hold the
    abort for 60s while the entry point was already dark.
    """
    base = _spec(mode=AdaptiveMode.EVALUATION).model_dump()
    base["abort"] = {
        "match": "any",
        "conditions": [
            # Polled every tick, and dark right now.
            {"id": "entry-unreachable", "signal": "health", "operator": "eq", "value": False},
            # A 60s metric that is sitting quiet — it must not set the pace.
            {"id": "network-errors", "signal": "db_lock", "operator": "eq", "value": True},
        ],
        "consecutive_ticks": 2,
    }
    spec = AdaptiveSpec.model_validate(base)
    signals = {
        "error_rate": _value(0.0),
        "health": {**_value(False), "update_interval_sec": 0},
        "db_lock": {**_value(False), "update_interval_sec": 60},
    }
    state = start(spec)
    state = advance(spec, state, Observation(elapsed_sec=5, signals=signals))
    assert state.streaks["abort"] == 1
    state = advance(spec, state, Observation(elapsed_sec=20, signals=signals))
    assert state.phase is ControllerPhase.ABORTED


def test_streak_resets_rather_than_holds_when_the_condition_stops_matching() -> None:
    """Holding a streak across a stale read must not survive a genuine miss."""
    spec = _spec(mode=AdaptiveMode.EVALUATION)
    state = start(spec)
    state = advance(spec, state, Observation(elapsed_sec=15, signals=_safe_signals(
        error_rate=0.2, update_interval_sec=60)))
    assert state.streaks["success"] == 1
    state = advance(spec, state, Observation(elapsed_sec=30, signals=_safe_signals(
        error_rate=0.0, update_interval_sec=60)))
    assert state.streaks["success"] == 0
    state = advance(spec, state, Observation(elapsed_sec=45, signals=_safe_signals(
        error_rate=0.2, update_interval_sec=60)))
    assert state.streaks["success"] == 1


def test_schema_matches_calibration_and_evaluation_contract() -> None:
    assert _spec().mode == AdaptiveMode.CALIBRATION
    assert _spec(mode=AdaptiveMode.EVALUATION).mode == AdaptiveMode.EVALUATION

    with pytest.raises(ValidationError, match="evaluation mode requires exactly one level"):
        AdaptiveSpec.model_validate({**_spec().model_dump(), "mode": "evaluation"})
    with pytest.raises(ValidationError, match="multi-level calibration requires escalate"):
        AdaptiveSpec.model_validate({**_spec().model_dump(), "escalate": None})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AdaptiveLevel(id="one", settle_sec=0, timeout_sec=1, unexpected=True)


def test_level_parameters_accept_nested_exact_restore_contracts() -> None:
    level = AdaptiveLevel(
        id="probe-fault",
        parameters={
            "baseline": {"httpGet": {"path": "/actuator/health", "port": 8083}},
            "fault": {"httpGet": {"path": "/actuator/health/fail", "port": 8083}},
        },
        settle_sec=0,
        timeout_sec=60,
    )

    assert level.parameters["baseline"]["httpGet"]["port"] == 8083


def test_schema_rejects_bad_windows_and_comparisons() -> None:
    with pytest.raises(ValidationError, match="timeout_sec must be"):
        AdaptiveLevel(id="bad", min_hold_sec=11, settle_sec=5, timeout_sec=10)
    with pytest.raises(ValidationError, match="ordered comparison requires a number"):
        ConditionSet(
            conditions=[
                {"id": "bad", "signal": "x", "operator": "gt", "value": "high"}
            ]
        )


def test_settle_and_min_hold_gate_success() -> None:
    spec = _spec()
    state = start(spec)
    settling = advance(spec, state, Observation(elapsed_sec=4, signals=_safe_signals()))
    assert settling.phase == ControllerPhase.SETTLING
    assert settling.reason == "settling"

    holding = advance(spec, settling, Observation(elapsed_sec=6, signals=_safe_signals()))
    assert holding.phase == ControllerPhase.EVALUATING
    assert holding.reason == "min_hold"


def test_must_rule_out_deferred_until_after_settle_and_min_hold() -> None:
    # Injections that deliberately restart the target pod (k8s.env, k8s.probe)
    # drive the alternative-cause signal true transiently during the expected
    # restart. must_rule_out must not veto the run before the injection settles
    # past min_hold; a signal that clears before min_hold must still succeed.
    spec = _spec()
    state = start(spec)

    settling = advance(
        spec, state, Observation(elapsed_sec=4, signals=_safe_signals(db_lock=True))
    )
    assert settling.phase == ControllerPhase.SETTLING
    assert settling.reason == "settling"

    holding = advance(
        spec, settling, Observation(elapsed_sec=6, signals=_safe_signals(db_lock=True))
    )
    assert holding.phase == ControllerPhase.EVALUATING
    assert holding.reason == "min_hold"

    # Signal clears before min_hold (pod recovered) -> success, not abort.
    first = advance(
        spec, holding, Observation(elapsed_sec=10, signals=_safe_signals(error_rate=0.1))
    )
    assert first.phase == ControllerPhase.EVALUATING
    succeeded = advance(
        spec, first, Observation(elapsed_sec=11, signals=_safe_signals(error_rate=0.1))
    )
    assert succeeded.phase == ControllerPhase.SUCCEEDED


def test_must_rule_out_persisting_past_min_hold_still_aborts() -> None:
    # A genuine alternative cause (pod never recovers) keeps the streak alive
    # past min_hold and must still abort, preserving failure detection.
    spec = _spec()
    state = start(spec)
    holding = advance(
        spec, state, Observation(elapsed_sec=6, signals=_safe_signals(db_lock=True))
    )
    assert holding.phase == ControllerPhase.EVALUATING
    aborted = advance(
        spec, holding, Observation(elapsed_sec=10, signals=_safe_signals(db_lock=True))
    )
    assert aborted.phase == ControllerPhase.ABORTED
    assert aborted.reason == "must_rule_out_detected"


def test_must_rule_out_true_aborts_and_false_allows_success() -> None:
    spec = _spec()
    state = start(spec)
    ruled_out = advance(
        spec,
        state,
        Observation(elapsed_sec=10, signals=_safe_signals(error_rate=0.1, db_lock=True)),
    )
    assert ruled_out.phase == ControllerPhase.ABORTED
    assert ruled_out.reason == "must_rule_out_detected"

    first = advance(
        spec,
        state,
        Observation(elapsed_sec=10, signals=_safe_signals(error_rate=0.1)),
    )
    assert first.phase == ControllerPhase.EVALUATING
    succeeded = advance(
        spec,
        first,
        Observation(elapsed_sec=11, signals=_safe_signals(error_rate=0.1)),
    )
    assert succeeded.phase == ControllerPhase.SUCCEEDED


def test_unknown_ruleout_blocks_success_then_aborts_at_timeout() -> None:
    spec = _spec()
    signals = _safe_signals(error_rate=0.1)
    signals["db_lock"] = _value(False, freshness="stale")
    pending = advance(spec, start(spec), Observation(elapsed_sec=10, signals=signals))
    assert pending.phase == ControllerPhase.EVALUATING
    assert pending.reason == "safety_observation_pending"

    timed_out = advance(spec, pending, Observation(elapsed_sec=30, signals=signals))
    assert timed_out.phase == ControllerPhase.ABORTED
    assert timed_out.reason == "safety_observation_unavailable"


def test_stale_abort_signal_prevents_escalation() -> None:
    spec = _spec()
    signals = _safe_signals(error_rate=0.0)
    signals["health"] = _value(True, quality="error")
    first = advance(spec, start(spec), Observation(elapsed_sec=10, signals=signals))
    second = advance(spec, first, Observation(elapsed_sec=11, signals=signals))
    assert second.phase == ControllerPhase.EVALUATING
    assert second.action == ControllerAction.WAIT
    timed_out = advance(spec, second, Observation(elapsed_sec=30, signals=signals))
    assert timed_out.reason == "safety_observation_unavailable"


def test_abort_consecutive_ticks_and_priority() -> None:
    spec = _spec()
    state = start(spec)
    first = advance(
        spec,
        state,
        Observation(
            elapsed_sec=10,
            signals=_safe_signals(error_rate=0.1, health=False),
        ),
    )
    assert first.phase == ControllerPhase.EVALUATING
    assert first.reason == "safety_observation_pending"
    second = advance(
        spec,
        first,
        Observation(
            elapsed_sec=11,
            signals=_safe_signals(error_rate=0.1, health=False),
        ),
    )
    assert second.phase == ControllerPhase.ABORTED
    assert second.reason == "abort_condition"


def test_calibration_escalation_uses_consecutive_ticks_and_resets_level_time() -> None:
    spec = _spec()
    low = start(spec)
    first = advance(
        spec, low, Observation(elapsed_sec=10, signals=_safe_signals(error_rate=0.0))
    )
    high = advance(
        spec, first, Observation(elapsed_sec=11, signals=_safe_signals(error_rate=0.0))
    )
    assert high.phase == ControllerPhase.SETTLING
    assert high.level_id == "high"
    assert high.last_elapsed_sec == 0
    assert high.streaks == {}


def test_evaluation_never_escalates() -> None:
    spec = _spec(mode=AdaptiveMode.EVALUATION)
    state = start(spec)
    first = advance(
        spec, state, Observation(elapsed_sec=15, signals=_safe_signals(error_rate=0.0))
    )
    failed = advance(
        spec, first, Observation(elapsed_sec=16, signals=_safe_signals(error_rate=0.0))
    )
    assert failed.phase == ControllerPhase.FAILED
    assert failed.reason == "evaluation_level_insufficient"


def test_level_elapsed_time_must_be_monotonic() -> None:
    spec = _spec()
    state = advance(
        spec,
        start(spec),
        Observation(elapsed_sec=10, signals=_safe_signals(error_rate=0.0)),
    )
    with pytest.raises(ValueError, match="must be monotonic"):
        advance(
            spec,
            state,
            Observation(elapsed_sec=9, signals=_safe_signals(error_rate=0.0)),
        )


def test_calibration_timeout_escalates_when_safety_is_fresh() -> None:
    spec = _spec()
    result = advance(
        spec,
        start(spec),
        Observation(elapsed_sec=30, signals=_safe_signals(error_rate=0.02)),
    )
    assert result.action == ControllerAction.ESCALATE
    assert result.reason == "level_timeout"


def test_cleanup_failure_creates_dirty_outcome() -> None:
    terminal = ControllerState(
        phase=ControllerPhase.SUCCEEDED, level_index=0, level_id="low"
    )
    dirty = finalize_cleanup(terminal, cleanup_succeeded=False)
    assert dirty.phase == ControllerPhase.DIRTY
    assert dirty.dirty is True
    with pytest.raises(ValueError, match="explicit recovery"):
        finalize_cleanup(dirty, cleanup_succeeded=True)


def test_transition_is_side_effect_free_and_repeatable() -> None:
    spec = _spec()
    original = start(spec)
    observation = Observation(elapsed_sec=10, signals=_safe_signals(error_rate=0.0))
    first = advance(spec, original, observation)
    second = advance(spec, original, observation)
    assert first == second
    assert original.last_elapsed_sec == 0
    assert original.streaks == {}
