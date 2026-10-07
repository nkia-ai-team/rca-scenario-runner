"""The pass kind is one decision, and everything case-related follows from it.

On 2026-07-31 these behaviours were switched off one at a time during a live
batch, each discovered only after it had already stopped the queue. The point of
the mode is that there is no seventh switch to discover: if a behaviour exists to
produce or protect the eval case, it belongs here and turns off with the rest.
"""
from __future__ import annotations

from datetime import timedelta

from app.pass_mode import (
    DATASET,
    SMOKE,
    capture_enabled,
    golden_reset_enabled,
    inter_scenario_gap,
    is_smoke_pass,
    isolation_checks_enabled,
    pass_mode,
    protection_window_enabled,
)


CASE_RELATED = (capture_enabled, protection_window_enabled, isolation_checks_enabled)


def test_smoke_pass_turns_off_every_case_related_behaviour_together(monkeypatch) -> None:
    monkeypatch.setenv("SCENARIO_PASS", "smoke")
    assert pass_mode() == SMOKE and is_smoke_pass()
    assert [behaviour() for behaviour in CASE_RELATED] == [False, False, False]
    assert inter_scenario_gap() == timedelta(minutes=5)
    # the operator's own golden-reset setting is overridden off, not consulted
    assert golden_reset_enabled(True) is False


def test_dataset_is_the_default_and_keeps_everything(monkeypatch) -> None:
    """Defaulting the other way would silently drop captures on a real run, and
    the fault window is gone once it passes — a smoke pass that does too much
    only costs time."""
    monkeypatch.delenv("SCENARIO_PASS", raising=False)
    assert pass_mode() == DATASET and not is_smoke_pass()
    assert [behaviour() for behaviour in CASE_RELATED] == [True, True, True]
    assert inter_scenario_gap() == timedelta(minutes=30)
    assert golden_reset_enabled(True) is True
    assert golden_reset_enabled(False) is False


def test_only_the_exact_word_smoke_releases_anything(monkeypatch) -> None:
    """A typo must fail safe: "smoke-test" or "SMOKE_PASS" reads as dataset."""
    for value in ("", "smoke-test", "true", "1", "off", "SMOKE_PASS"):
        monkeypatch.setenv("SCENARIO_PASS", value)
        assert pass_mode() == DATASET, value
        assert [behaviour() for behaviour in CASE_RELATED] == [True, True, True], value
    # case and surrounding whitespace are not typos
    for value in ("smoke", " SMOKE ", "Smoke"):
        monkeypatch.setenv("SCENARIO_PASS", value)
        assert pass_mode() == SMOKE, value


def test_dataset_gap_cannot_be_tuned_below_the_capture_window_sum(monkeypatch) -> None:
    """15m was tried live and the runner's isolation gates blocked every start.

    The capture window is [t1-10m, t2+20m], so a gap under 30m overlaps the
    neighbouring case; each manifest re-enforces it with a ge=1800 floor. The
    env var is kept for raising the gap, not lowering it — a smoke pass is the
    supported way to go faster.
    """
    monkeypatch.setenv("SCENARIO_PASS", "dataset")
    monkeypatch.setenv("SCENARIO_CLEAN_WINDOW_MIN", "15")
    assert inter_scenario_gap() >= timedelta(minutes=30)
    monkeypatch.setenv("SCENARIO_CLEAN_WINDOW_MIN", "120")
    assert inter_scenario_gap() == timedelta(minutes=120)
