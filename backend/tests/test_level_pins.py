from __future__ import annotations

import json

import pytest

from app.adaptive import ControllerPhase
from app.adaptive_runtime import AdaptiveRuntime, SessionStatus
from app.level_pins import (
    load_level_pins,
    pin_controller_spec,
    resolve_pinned_level,
)

from tests.test_adaptive_runtime import (
    FakeApplier,
    FakeClock,
    FakeEligibility,
    _poller,
    _spec,
)


def _pinned_runtime(
    clock: FakeClock, values: dict[str, object], *, level_id: str
) -> tuple[AdaptiveRuntime, FakeApplier]:
    applier = FakeApplier(clock)
    runtime = AdaptiveRuntime.create(
        run_id="run-1",
        scenario_id="F07-H",
        fencing_token=7,
        profile_id="load.north_south.v1",
        approved_profile_id=None,
        spec=pin_controller_spec(_spec(), level_id),
        clock=clock,
        eligibility_probe=FakeEligibility(clock),
        poller=_poller(clock, values),
        applier=applier,
        pinned_level_id=level_id,
    )
    return runtime, applier


def test_load_level_pins_missing_registry_means_no_pins(tmp_path) -> None:
    assert load_level_pins(tmp_path / "level-pins.json") == {}


def test_load_level_pins_reads_the_registry_document(tmp_path, monkeypatch) -> None:
    path = tmp_path / "level-pins.json"
    path.write_text(
        json.dumps({"schema_version": 1, "pins": {"F05-R": "l2-mem-640mi"}}),
        encoding="utf-8",
    )
    assert load_level_pins(path) == {"F05-R": "l2-mem-640mi"}
    monkeypatch.setenv("LEVEL_PINS_PATH", str(path))
    assert load_level_pins() == {"F05-R": "l2-mem-640mi"}


def test_load_level_pins_fails_closed_on_a_malformed_registry(tmp_path) -> None:
    path = tmp_path / "level-pins.json"
    # A broken registry must raise, not quietly fall back to ladder search —
    # that would reintroduce the exact variance the pin removes.
    path.write_text(json.dumps({"pins": ["F05-R"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="keyed by scenario id"):
        load_level_pins(path)
    path.write_text(json.dumps({"pins": {"F05-R": ""}}), encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty level id"):
        load_level_pins(path)


def test_resolve_pinned_level_prefers_scenario_id_over_slug() -> None:
    pins = {"F07-H": "high", "f07-h-surge": "low"}
    assert resolve_pinned_level(pins, scenario_id="F07-H", catalog_slug="f07-h-surge") == "high"
    assert resolve_pinned_level(pins, scenario_id="OTHER", catalog_slug="f07-h-surge") == "low"
    assert resolve_pinned_level(pins, scenario_id="OTHER", catalog_slug=None) is None


def test_pin_controller_spec_keeps_only_the_pinned_level() -> None:
    spec = _spec()
    pinned = pin_controller_spec(spec, "high")
    assert [level.id for level in pinned.adaptive.levels] == ["high"]
    assert pinned.adaptive.levels[0] == spec.adaptive.levels[1]
    # Everything outside the ladder is untouched.
    assert pinned.adaptive.success == spec.adaptive.success
    assert pinned.recovery == spec.recovery
    assert [level.id for level in spec.adaptive.levels] == ["low", "high"]


def test_pin_controller_spec_rejects_an_unknown_level() -> None:
    with pytest.raises(ValueError, match="not in the scenario ladder"):
        pin_controller_spec(_spec(), "l9-missing")


async def test_pinned_run_enters_the_pinned_level_directly() -> None:
    clock = FakeClock()
    values = {
        "loadgen.achieved_rps": 100,
        "http.entry_health": 200,
        "database.tagged_session_count": 0,
    }
    runtime, applier = _pinned_runtime(clock, values, level_id="high")

    session = await runtime.begin()
    assert len(applier.applies) == 1
    assert applier.applies[0].level_id == "high"
    assert applier.applies[0].parameters == {"target_rps": 80}
    assert session.controller_state and session.controller_state.level_index == 0

    clock.advance(20)
    await runtime.tick()
    clock.advance(30)
    session = await runtime.tick()

    assert session.status == SessionStatus.CLEAN
    assert session.controller_state and session.controller_state.phase == ControllerPhase.SUCCEEDED
    assert len(applier.applies) == 1
    assert session.pinned_level_id == "high"
    assert session.model_dump(mode="json")["pinned_level_id"] == "high"
    assert session.trusted_evidence()["pinned_level_id"] == "high"


async def test_unpinned_run_keeps_the_existing_ladder_entry() -> None:
    clock = FakeClock()
    applier = FakeApplier(clock)
    runtime = AdaptiveRuntime.create(
        run_id="run-1",
        scenario_id="F07-H",
        fencing_token=7,
        profile_id="load.north_south.v1",
        approved_profile_id=None,
        spec=_spec(),
        clock=clock,
        eligibility_probe=FakeEligibility(clock),
        poller=_poller(clock, {"http.entry_health": 200}),
        applier=applier,
    )

    session = await runtime.begin()

    assert applier.applies[0].level_id == "low"
    assert session.pinned_level_id is None
    assert session.trusted_evidence()["pinned_level_id"] is None


async def test_pinned_level_failure_does_not_resume_exploring() -> None:
    clock = FakeClock()
    values = {
        # "low" alone cannot meet success; the unpinned ladder escalates to
        # "high" here (test_calibration_escalates_after_fresh_consecutive_ticks).
        "loadgen.achieved_rps": 50,
        "http.entry_health": 200,
        "database.tagged_session_count": 0,
    }
    runtime, applier = _pinned_runtime(clock, values, level_id="low")
    await runtime.begin()

    clock.advance(20)
    await runtime.tick()
    clock.advance(30)
    session = await runtime.tick()

    assert session.controller_state and session.controller_state.phase == ControllerPhase.FAILED
    assert session.controller_state.reason == "calibration_levels_exhausted"
    assert len(applier.applies) == 1
    assert not applier.transition_cleanups
    assert len(applier.cleanups) == 1
    assert session.pinned_level_id == "low"
