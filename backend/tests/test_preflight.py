from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.preflight import (
    AiJudgement,
    PreflightCheckInput,
    evaluate_preflight,
)

NOW = datetime(2026, 7, 16, 1, 0, 0, tzinfo=timezone.utc)
WINDOW = ("2026-07-16T00:50:00Z", "2026-07-16T01:00:00Z")


def _r6_checks(*, five_xx: float = 0.0, p95: float = 0.4, pool_pending: float = 0.0):
    # A representative R6 deterministic check set for the [t1-10m, t1] window.
    return [
        PreflightCheckInput(name="baseline_loadgen_alive", value=1, threshold=1, op="gte"),
        PreflightCheckInput(name="diurnal_band_rps", value=8, threshold=5, op="gte"),
        PreflightCheckInput(name="user_5xx_rate", value=five_xx, threshold=0.01, op="lte", tolerance=0.01),
        PreflightCheckInput(name="entry_p95_sec", value=p95, threshold=0.5, op="lte"),
        PreflightCheckInput(name="active_alarms", value=0, threshold=0, op="eq"),
        PreflightCheckInput(name="open_incidents", value=0, threshold=0, op="eq"),
        PreflightCheckInput(name="prev_pool_pending", value=pool_pending, threshold=0, op="eq"),
    ]


class RecordingJudge:
    def __init__(self, clean: bool) -> None:
        self.clean = clean
        self.calls = 0

    def adjudicate(self, *, window, checks):
        self.calls += 1
        return AiJudgement(clean=self.clean, rationale="borderline reviewed")


def test_all_pass_is_clean() -> None:
    verdict = evaluate_preflight(window=WINDOW, checks=_r6_checks(), now=NOW)
    assert verdict.verdict == "clean"
    assert verdict.is_clean
    assert verdict.checked_at == "2026-07-16T01:00:00Z"
    assert all(check.passed for check in verdict.checks)


def test_wait_promotes_to_clean_after_wait() -> None:
    verdict = evaluate_preflight(window=WINDOW, checks=_r6_checks(), now=NOW, waited_sec=300)
    assert verdict.verdict == "clean_after_wait"
    assert verdict.is_clean


def test_hard_failure_is_dirty_and_bypasses_ai() -> None:
    judge = RecordingJudge(clean=True)
    verdict = evaluate_preflight(
        window=WINDOW,
        checks=_r6_checks(p95=5.0),  # far past threshold+tolerance -> hard fail
        now=NOW,
        ai_judge=judge,
    )
    assert verdict.verdict == "dirty"
    assert not verdict.is_clean
    assert judge.calls == 0


def test_borderline_without_ai_is_dirty() -> None:
    # 5xx 0.015 fails the 0.01 threshold but is within 0.01 tolerance -> borderline.
    verdict = evaluate_preflight(window=WINDOW, checks=_r6_checks(five_xx=0.015), now=NOW)
    assert verdict.verdict == "dirty"
    assert any(check.status == "borderline" for check in verdict.checks)


def test_borderline_cleared_by_ai_is_ai_judged_clean() -> None:
    judge = RecordingJudge(clean=True)
    verdict = evaluate_preflight(
        window=WINDOW, checks=_r6_checks(five_xx=0.015), now=NOW, ai_judge=judge
    )
    assert verdict.verdict == "ai_judged_clean"
    assert verdict.is_clean
    assert judge.calls == 1
    assert verdict.ai_judgement is not None and verdict.ai_judgement.clean


def test_borderline_rejected_by_ai_is_dirty() -> None:
    judge = RecordingJudge(clean=False)
    verdict = evaluate_preflight(
        window=WINDOW, checks=_r6_checks(five_xx=0.015), now=NOW, ai_judge=judge
    )
    assert verdict.verdict == "dirty"
    assert judge.calls == 1


def test_to_meta_matches_capture_contract() -> None:
    verdict = evaluate_preflight(window=WINDOW, checks=_r6_checks(), now=NOW, waited_sec=0)
    meta = verdict.to_meta()
    assert set(meta) == {"window", "verdict", "checked_at", "waited_sec", "checks", "ai_judgement"}
    assert meta["window"] == list(WINDOW)
    assert meta["ai_judgement"] is None
    first = meta["checks"][0]
    assert set(first) == {"name", "value", "threshold", "pass"}
    assert first["pass"] is True


def test_naive_clock_and_empty_checks_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_preflight(window=WINDOW, checks=_r6_checks(), now=datetime(2026, 7, 16, 1, 0, 0))
    with pytest.raises(ValueError, match="at least one check"):
        evaluate_preflight(window=WINDOW, checks=[], now=NOW)
