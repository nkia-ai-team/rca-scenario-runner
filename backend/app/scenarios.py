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

from app.execution_policy import validate_execution_plan
from app.controller import parse_controller
from app.models import Domain, ExecutionPlan, Scenario

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


def _normalize_root_cause(entry: dict) -> tuple[str, dict | None]:
    """Legacy: root_cause is a display string. Structured: an object whose
    `mechanism` becomes the display string and the full object is preserved."""
    raw = entry["root_cause"]
    if isinstance(raw, dict):
        mechanism = raw.get("mechanism")
        display = str(mechanism).strip() if mechanism else ""
        if not display:
            display = str(raw.get("target_kind", "")).strip() or "(root_cause 객체 — mechanism 미기재)"
        return display, raw
    return str(raw), None


def _normalize_propagation(entry: dict) -> tuple[str, list[str] | None]:
    """Legacy: a single arrow-joined string. Structured: a list of steps."""
    raw = entry["propagation"]
    if isinstance(raw, list):
        steps = [str(s).strip() for s in raw if str(s).strip()]
        return " → ".join(steps), steps
    return str(raw), None


def _derive_expected_alarms(entry: dict, anomalies: list[dict] | None) -> list[str]:
    """Structured entries carry expected_anomalies (load-spec §3) instead of the
    legacy expected_alarms strings — derive display strings so the UI list stays populated."""
    alarms = entry.get("expected_alarms", [])
    if alarms or not anomalies:
        return alarms
    derived = []
    for a in anomalies:
        if not isinstance(a, dict):
            continue
        parts = [str(a.get("name", "?"))]
        if a.get("target"):
            parts.insert(0, str(a["target"]))
        if a.get("within"):
            parts.append(f"within {a['within']}")
        derived.append(" — ".join(parts[:2]) + (f" ({parts[2]})" if len(parts) > 2 else ""))
    return derived


def _spec_entry_to_scenario(domain: str, domain_label: str, entry: dict) -> Scenario:
    """Map one scenario entry (legacy flat or structured) to the Scenario model."""
    short_id = _normalize_short_id(entry["id"])
    difficulty = entry.get("difficulty")
    if not (isinstance(difficulty, int) and 1 <= difficulty <= 5):
        difficulty = None  # out-of-range or non-int treated as unset
    expected = entry.get("expected_rca_root_cause")
    if isinstance(expected, str):
        expected = expected.strip() or None
    else:
        expected = None
    cause, root_cause_detail = _normalize_root_cause(entry)
    propagation, propagation_steps = _normalize_propagation(entry)
    anomalies = entry.get("expected_anomalies")
    if not isinstance(anomalies, list):
        anomalies = None
    execution_raw = entry.get("execution")
    if execution_raw is None:
        execution_raw = {
            "transport": "local",
            "location": "scenario-runner",
            "timeout_sec": 600,
        }
    if not isinstance(execution_raw, dict):
        raise ValueError(f"scenario {domain}:{short_id} execution must be a mapping")
    if "orchestrator" not in execution_raw:
        execution_raw = {"orchestrator": execution_raw, "injection_points": []}
    execution = ExecutionPlan.model_validate(execution_raw)
    validate_execution_plan(execution.injection_points)
    return Scenario(
        id=_composite_id(domain, short_id),
        short_id=short_id,
        domain=domain,
        domain_label=domain_label,
        name=entry["title"],
        description=entry["description"],
        cause=cause,
        propagation=propagation,
        expected_alarms=_derive_expected_alarms(entry, anomalies),
        estimated_duration_sec=entry["estimated_duration_sec"],
        script_filename=entry.get("file") or entry.get("injection", {}).get("script"),
        execution=execution,
        warnings=entry.get("side_effects", []),
        difficulty=difficulty,
        expected_rca_root_cause=expected,
        cause_domain=entry.get("cause_domain"),
        expected_depth=entry.get("expected_depth"),
        root_cause_detail=root_cause_detail,
        propagation_steps=propagation_steps,
        injection=entry.get("injection") if isinstance(entry.get("injection"), dict) else None,
        expected_anomalies=anomalies,
        signals=entry.get("signals") if isinstance(entry.get("signals"), dict) else None,
        expected_clusters=(
            entry.get("expected_clusters")
            if isinstance(entry.get("expected_clusters"), dict)
            else None
        ),
        expected_incidents=(
            entry.get("expected_incidents")
            if isinstance(entry.get("expected_incidents"), dict)
            else None
        ),
        controller=parse_controller(entry.get("controller")),
    )


def _add_to_catalog(catalog: dict[str, Scenario], scenario: Scenario, source: Path) -> None:
    if scenario.id in catalog:
        raise ValueError(
            f"duplicate scenario id '{scenario.id}' (second definition in {source})"
        )
    catalog[scenario.id] = scenario


def _load_scenarios() -> dict[str, Scenario]:
    """Two layouts per domain, both supported (spec-scenario-design §10):

    - legacy: entries inline under service-spec.yaml `scenarios:` list
    - per-scenario: one YAML file per scenario under `<domain>/scenarios/*.yaml`,
      whose top level is a single scenario entry mapping

    Duplicate composite ids across the two sources are a hard error.
    """
    root = _resolve_scenarios_root()
    catalog: dict[str, Scenario] = {}
    if not root.is_dir():
        return catalog
    for spec_file in sorted(root.glob("*/service-spec.yaml")):
        domain = spec_file.parent.name
        with spec_file.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        domain_label = _domain_label(domain, data)
        for entry in data.get("scenarios", []) or []:
            scenario = _spec_entry_to_scenario(domain, domain_label, entry)
            _add_to_catalog(catalog, scenario, spec_file)
        for sc_file in sorted((spec_file.parent / "scenarios").glob("*.yaml")):
            with sc_file.open("r", encoding="utf-8") as f:
                entry = yaml.safe_load(f) or {}
            if not isinstance(entry, dict) or "id" not in entry:
                raise ValueError(f"per-scenario file must be a single scenario mapping with 'id': {sc_file}")
            scenario = _spec_entry_to_scenario(domain, domain_label, entry)
            _add_to_catalog(catalog, scenario, sc_file)
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
    if "/" in scenario_id:
        # golden/meta.json canonical form "commerce/scenario-01" -> "commerce:01"
        domain, _, raw = scenario_id.partition("/")
        return _composite_id(domain, _normalize_short_id(raw))
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
