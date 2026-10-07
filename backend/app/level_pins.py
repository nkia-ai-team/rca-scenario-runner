"""Per-scenario ladder pins for deterministic capture replays.

The adaptive ladder exists to *find* a working intensity; a capture replay
already knows it. A pin registry maps a scenario id to one approved level id,
and a pinned run enters that level directly instead of climbing. The pinned
spec keeps exactly one calibration level, so an escalate confirmation or a
level timeout terminates through the existing ``calibration_levels_exhausted``
path — a pinned level that does not reproduce the judgement fails, it never
resumes exploring (capture is a replay, so determinism outranks recall).

The registry is a JSON document next to the other approved registries and is
keyed by scenario id, like them:

    {"schema_version": 1, "pins": {"F05-R": "l2-mem-640mi"}}

A missing registry file means no pins (the pre-pin behaviour, unchanged). A
registry that exists but cannot be parsed, or a pin naming a level the
scenario does not have, raises instead of silently falling back to the
ladder: a capture batch that quietly resumed exploring would reintroduce the
exact run-to-run variance the pin was added to remove.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from app.controller import ControllerSpec

DEFAULT_LEVEL_PINS_PATH = Path("/opt/lucida/scenario-contracts/registry/level-pins.json")


def level_pins_path() -> Path:
    return Path(os.environ.get("LEVEL_PINS_PATH") or DEFAULT_LEVEL_PINS_PATH)


def load_level_pins(path: Path | None = None) -> dict[str, str]:
    """Return the scenario_id -> level_id pin mapping; {} when no registry exists."""
    path = path or level_pins_path()
    if not path.is_file():
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("level pin registry document must be an object")
    pins = document.get("pins")
    if not isinstance(pins, dict):
        raise ValueError("level pin registry requires a pins object keyed by scenario id")
    for scenario_id, level_id in pins.items():
        if not isinstance(scenario_id, str) or not scenario_id:
            raise ValueError("level pin scenario ids must be non-empty strings")
        if not isinstance(level_id, str) or not level_id:
            raise ValueError(f"level pin for {scenario_id} must be a non-empty level id")
    return dict(pins)


def resolve_pinned_level(
    pins: dict[str, str], *, scenario_id: str, catalog_slug: str | None = None
) -> str | None:
    """Look a scenario up by its runtime id first, then by its catalog slug."""
    pin = pins.get(scenario_id)
    if pin is None and catalog_slug is not None:
        pin = pins.get(catalog_slug)
    return pin


def pin_controller_spec(spec: ControllerSpec, level_id: str) -> ControllerSpec:
    """Return a spec whose ladder is exactly the pinned level, all else unchanged.

    Evaluation mode already fixes its single level; a pin naming that level is
    a no-op and any other id is the same contradiction as an unknown
    calibration level, so both fail closed below.
    """
    matches = [level for level in spec.adaptive.levels if level.id == level_id]
    if not matches:
        known = [level.id for level in spec.adaptive.levels]
        raise ValueError(f"pinned level {level_id} is not in the scenario ladder {known}")
    return spec.model_copy(
        update={"adaptive": spec.adaptive.model_copy(update={"levels": matches}, deep=True)},
        deep=True,
    )
