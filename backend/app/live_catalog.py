"""Web UI catalog for the current (manifest-backed) scenarios.

The cards the browser lists used to come only from the in-repo
``service-spec.yaml`` files, which describe earlier testbed generations
(plopvape-shop, social-feed). What actually runs today are the externally
maintained manifests under ``SCENARIO_MANIFEST_ROOT`` together with their
registry metadata. This module turns every live-enabled manifest into the same
``Scenario`` shape, so the UI lists exactly what
``POST /api/scenarios/{id}/run`` can start.

Nothing here decides whether a run is allowed — ``ScenarioRunner.start`` still
resolves the manifest itself. A manifest without registry metadata is left out
because the runner would refuse it anyway.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.manifests import ScenarioManifest, load_manifests
from app.models import Domain, Scenario

_DOMAIN_LABELS = {
    "commerce": "Commerce",
    "core-banking": "Core Banking",
    "food-delivery": "Food Delivery",
}
_UNASSIGNED_DOMAIN = "unassigned"
# Registry difficulty is a word; the card renders 1~5 stars.
_DIFFICULTY = {"easy": 2, "low": 2, "medium": 3, "중": 3, "high": 4, "상": 4}
_ID_PATTERN = re.compile(r"^F(\d+)-(.+)$")
_ARROW = re.compile(r"\s*(?:→|->)\s*")


def _load_metadata(path: Path) -> dict[str, dict[str, Any]]:
    """Raw registry entries. The capture model keeps only six fields; the cards
    need the design fields too (``*_ko``, ``must_support``, ``scoring``)."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    scenarios = document.get("scenarios") if isinstance(document, dict) else None
    if not isinstance(scenarios, dict):
        return {}
    return {key: value for key, value in scenarios.items() if isinstance(value, dict)}


def _text(meta: dict[str, Any], key: str) -> str:
    """Korean text when the registry has it, the canonical English otherwise."""
    for candidate in (f"{key}_ko", key):
        value = meta.get(candidate)
        if isinstance(value, str) and value.strip():
            return value.replace("**", "").strip()
    return ""


def _strings(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item).replace("**", "").strip() for item in raw if str(item).strip()]


def _grading_text(meta: dict[str, Any]) -> str | None:
    parts = [_text(meta, "distinguishing_evidence")]
    rule_out = _strings(meta.get("must_rule_out"))
    if rule_out:
        parts.append("배제할 것:\n" + "\n".join(f"- {item}" for item in rule_out))
    scoring = meta.get("scoring")
    if isinstance(scoring, dict):
        accept = _strings(scoring.get("accept"))
        partial = _strings(scoring.get("partial"))
        if accept:
            parts.append("정답 대상: " + ", ".join(accept))
        if partial:
            parts.append("부분 점수 대상: " + ", ".join(partial))
    text = "\n\n".join(part for part in parts if part)
    return text or None


def _to_scenario(manifest: ScenarioManifest, meta: dict[str, Any]) -> Scenario | None:
    runtime = manifest.runtime_scenario()
    if runtime is None:
        return None
    domain = meta.get("domain")
    if not isinstance(domain, str) or not domain.strip():
        domain = _UNASSIGNED_DOMAIN
    chain = _text(meta, "cause")
    hops = [hop for hop in _ARROW.split(chain) if hop]
    observed_in = _strings(meta.get("observation_domains"))
    root_cause = meta.get("root_cause")
    stage = (manifest.model_extra or {}).get("stage")
    return Scenario(
        id=manifest.id,
        short_id=manifest.id,
        stage=stage if isinstance(stage, str) else None,
        domain=domain,
        domain_label=_DOMAIN_LABELS.get(domain, domain.replace("-", " ").title()),
        name=_text(meta, "title") or manifest.id,
        description=_text(meta, "description"),
        cause=hops[0] if hops else chain,
        propagation=chain,
        expected_alarms=_strings(meta.get("must_support")),
        estimated_duration_sec=runtime.controller.max_injection_duration_sec,
        script_filename="run-scenario.sh",
        execution=runtime.execution,
        difficulty=_DIFFICULTY.get(str(meta.get("difficulty", "")).strip().lower()),
        expected_rca_root_cause=_grading_text(meta),
        cause_domain=", ".join(observed_in) or None,
        root_cause_detail=root_cause if isinstance(root_cause, dict) else None,
        propagation_steps=hops or None,
        injection=runtime.injection,
        controller=runtime.controller,
    )


def _sort_key(scenario: Scenario) -> tuple[int, int, str]:
    match = _ID_PATTERN.match(scenario.id)
    if match is None:
        return (1, 0, scenario.id)
    return (0, int(match.group(1)), match.group(2))


def list_live_scenarios(metadata_path: Path) -> list[Scenario]:
    """Every live-enabled manifest that has registry metadata, in id order."""
    metadata = _load_metadata(metadata_path)
    if not metadata:
        return []
    unique = {manifest.slug: manifest for manifest in load_manifests().values()}
    scenarios = []
    for manifest in unique.values():
        meta = metadata.get(manifest.id)
        if meta is None:
            continue
        scenario = _to_scenario(manifest, meta)
        if scenario is not None:
            scenarios.append(scenario)
    return sorted(scenarios, key=_sort_key)


def domains_of(scenarios: list[Scenario]) -> list[Domain]:
    counts: dict[str, int] = {}
    labels: dict[str, str] = {}
    for scenario in scenarios:
        counts[scenario.domain] = counts.get(scenario.domain, 0) + 1
        labels[scenario.domain] = scenario.domain_label
    return [
        Domain(slug=slug, label=labels[slug], scenario_count=counts[slug])
        for slug in sorted(counts)
    ]
