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

from app.models import Domain, Scenario

# scenarios.py lives at <repo>/backend/app/scenarios.py — go up 3 to reach repo root.
_DEFAULT_SCENARIOS_ROOT = (
    Path(__file__).resolve().parent.parent.parent / "scenarios" / "services"
)


def _resolve_scenarios_root() -> Path:
    env = os.environ.get("SCENARIOS_ROOT")
    if env:
        return Path(env)
    return _DEFAULT_SCENARIOS_ROOT


def get_default_domain() -> str:
    """Domain used to resolve bare short_ids (e.g. '01') from legacy clients.

    Browser tabs that were open before the multi-domain deploy keep polling
    /api/scenarios/01/status — those resolve against this default so the
    in-flight plopvape session never breaks.
    """
    return os.environ.get("DEFAULT_DOMAIN", "plopvape-shop")


def _normalize_short_id(raw: str) -> str:
    """'scenario-01' -> '01'; already-normalized '01' -> '01'."""
    prefix = "scenario-"
    if raw.startswith(prefix):
        return raw[len(prefix):]
    return raw


def _domain_label(slug: str, data: dict) -> str:
    for key in ("label", "name", "title"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return slug.replace("-", " ").title()


def _composite_id(domain: str, short_id: str) -> str:
    return f"{domain}:{short_id}"


def _spec_entry_to_scenario(domain: str, domain_label: str, entry: dict) -> Scenario:
    """Map one item under service-spec.yaml's `scenarios:` list to the Scenario model."""
    short_id = _normalize_short_id(entry["id"])
    difficulty = entry.get("difficulty")
    if not (isinstance(difficulty, int) and 1 <= difficulty <= 5):
        difficulty = None  # out-of-range or non-int treated as unset
    expected = entry.get("expected_rca_root_cause")
    if isinstance(expected, str):
        expected = expected.strip() or None
    else:
        expected = None
    return Scenario(
        id=_composite_id(domain, short_id),
        short_id=short_id,
        domain=domain,
        domain_label=domain_label,
        name=entry["title"],
        description=entry["description"],
        cause=entry["root_cause"],
        propagation=entry["propagation"],
        expected_alarms=entry.get("expected_alarms", []),
        estimated_duration_sec=entry["estimated_duration_sec"],
        script_filename=entry["file"],
        warnings=entry.get("side_effects", []),
        difficulty=difficulty,
        expected_rca_root_cause=expected,
    )


def _load_scenarios() -> dict[str, Scenario]:
    root = _resolve_scenarios_root()
    catalog: dict[str, Scenario] = {}
    if not root.is_dir():
        return catalog
    for spec_file in sorted(root.glob("*/service-spec.yaml")):
        domain = spec_file.parent.name
        with spec_file.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        domain_label = _domain_label(domain, data)
        for entry in data.get("scenarios", []):
            scenario = _spec_entry_to_scenario(domain, domain_label, entry)
            catalog[scenario.id] = scenario
    return catalog


SCENARIOS: dict[str, Scenario] = _load_scenarios()


def _resolve_scenario_id(scenario_id: str) -> str:
    """Map a possibly-bare short_id ('01') to its composite form ('plopvape-shop:01').

    Composite IDs pass through. Bare short_ids are first tried against
    DEFAULT_DOMAIN; if no match, fall back to a unique short_id across the
    catalog (useful for tests / single-domain dev setups where the default
    differs from the fixture's domain name).
    """
    if ":" in scenario_id:
        return scenario_id
    default_composite = _composite_id(get_default_domain(), scenario_id)
    if default_composite in SCENARIOS:
        return default_composite
    matches = [k for k in SCENARIOS if k.endswith(f":{scenario_id}")]
    if len(matches) == 1:
        return matches[0]
    return default_composite  # let the caller see a 404 against the default


def get_scenario(scenario_id: str) -> Scenario | None:
    return SCENARIOS.get(_resolve_scenario_id(scenario_id))


def list_scenarios() -> list[Scenario]:
    return list(SCENARIOS.values())


def list_domains() -> list[Domain]:
    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
    for s in SCENARIOS.values():
        counts[s.domain] = counts.get(s.domain, 0) + 1
        labels[s.domain] = s.domain_label
    return [
        Domain(slug=slug, label=labels[slug], scenario_count=counts[slug])
        for slug in sorted(counts)
    ]


def reload_scenarios() -> dict[str, Scenario]:
    """Force re-read from disk. Returns the new catalog."""
    global SCENARIOS
    SCENARIOS = _load_scenarios()
    return SCENARIOS
