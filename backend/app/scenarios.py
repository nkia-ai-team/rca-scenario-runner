"""
Scenario catalog — data-driven loader.

`service-spec.yaml` files under `<repo_root>/scenarios/services/*/service-spec.yaml`
are the single source of truth. This module discovers them at import time and
converts each `scenarios[]` entry into a `Scenario` model.

Override the discovery root via the `SCENARIOS_ROOT` env var (useful for tests
or alternate deployments).
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from app.models import Scenario

# scenarios.py lives at <repo>/backend/app/scenarios.py — go up 3 to reach repo root.
_DEFAULT_SCENARIOS_ROOT = (
    Path(__file__).resolve().parent.parent.parent / "scenarios" / "services"
)


def _resolve_scenarios_root() -> Path:
    env = os.environ.get("SCENARIOS_ROOT")
    if env:
        return Path(env)
    return _DEFAULT_SCENARIOS_ROOT


def _normalize_id(raw: str) -> str:
    """'scenario-01' -> '01'; already-normalized '01' -> '01'."""
    prefix = "scenario-"
    if raw.startswith(prefix):
        return raw[len(prefix):]
    return raw


def _spec_entry_to_scenario(entry: dict) -> Scenario:
    """Map one item under service-spec.yaml's `scenarios:` list to the Scenario model."""
    return Scenario(
        id=_normalize_id(entry["id"]),
        name=entry["title"],
        description=entry["description"],
        cause=entry["root_cause"],
        propagation=entry["propagation"],
        expected_alarms=entry.get("expected_alarms", []),
        estimated_duration_sec=entry["estimated_duration_sec"],
        script_filename=entry["file"],
        warnings=entry.get("side_effects", []),
    )


def _load_scenarios() -> dict[str, Scenario]:
    root = _resolve_scenarios_root()
    catalog: dict[str, Scenario] = {}
    if not root.is_dir():
        return catalog
    for spec_file in sorted(root.glob("*/service-spec.yaml")):
        with spec_file.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for entry in data.get("scenarios", []):
            scenario = _spec_entry_to_scenario(entry)
            catalog[scenario.id] = scenario
    return catalog


SCENARIOS: dict[str, Scenario] = _load_scenarios()


def get_scenario(scenario_id: str) -> Scenario | None:
    return SCENARIOS.get(scenario_id)


def list_scenarios() -> list[Scenario]:
    return list(SCENARIOS.values())


def reload_scenarios() -> dict[str, Scenario]:
    """Force re-read from disk. Returns the new catalog."""
    global SCENARIOS
    SCENARIOS = _load_scenarios()
    return SCENARIOS
