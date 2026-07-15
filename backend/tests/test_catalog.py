"""Catalog loader regression tests against the real scenario specs in this repo.

Guards the two supported layouts (legacy inline list / per-scenario files) and
the structured scenario schema (testbed-services spec-scenario-design §4·§10).
"""
from pathlib import Path

import pytest

from app import scenarios as scenarios_module

REPO_SCENARIOS_ROOT = Path(__file__).resolve().parent.parent.parent / "scenarios" / "services"


@pytest.fixture
def real_catalog(monkeypatch):
    monkeypatch.setenv("SCENARIOS_ROOT", str(REPO_SCENARIOS_ROOT))
    catalog = scenarios_module.reload_scenarios()
    yield catalog
    monkeypatch.delenv("SCENARIOS_ROOT")
    scenarios_module.reload_scenarios()


def test_all_domains_load_without_validation_error(real_catalog):
    domains = {s.domain for s in real_catalog.values()}
    assert {"commerce", "plopvape-shop", "food-delivery", "social-feed"} <= domains


def test_commerce_structured_scenario_loads_from_per_scenario_file(real_catalog):
    s = real_catalog["commerce:01"]
    # display strings normalized from structured fields
    assert "HikariCP" in s.cause
    assert " → " in s.propagation
    # structured originals preserved
    assert s.root_cause_detail["target_kind"] == "service"
    assert s.propagation_steps and len(s.propagation_steps) == 4
    assert s.cause_domain == "APM"
    assert s.expected_depth == "entity"
    assert s.injection["parameters"]["load"]["intensity_multiplier"] == 8
    assert len(s.expected_anomalies) == 3
    assert s.signals["must_rule_out"]
    # expected_alarms derived from expected_anomalies for the UI
    assert len(s.expected_alarms) == 3
    assert s.script_filename == "scenario-01-blackfriday-surge.sh"


def test_legacy_flat_entries_still_load(real_catalog):
    s = real_catalog["plopvape-shop:01"]
    assert s.cause.startswith("장시간 SELECT FOR UPDATE")
    assert s.root_cause_detail is None
    assert s.propagation_steps is None
    assert s.expected_alarms  # legacy strings pass through


def test_slash_form_id_resolves(real_catalog, monkeypatch):
    monkeypatch.setattr(scenarios_module, "SCENARIOS", real_catalog)
    assert scenarios_module.get_scenario("commerce/scenario-01").id == "commerce:01"


def test_duplicate_scenario_id_is_hard_error(tmp_path, monkeypatch):
    domain = tmp_path / "dup-domain"
    (domain / "scenarios").mkdir(parents=True)
    (domain / "service-spec.yaml").write_text(
        "service: {name: dup-domain}\n"
        "scenarios:\n"
        "- id: scenario-01\n"
        "  file: a.sh\n"
        "  title: t\n"
        "  description: d\n"
        "  root_cause: r\n"
        "  propagation: p\n"
        "  estimated_duration_sec: 60\n",
        encoding="utf-8",
    )
    (domain / "scenarios" / "scenario-01-dup.yaml").write_text(
        "id: scenario-01\nfile: b.sh\ntitle: t\ndescription: d\n"
        "root_cause: r\npropagation: p\nestimated_duration_sec: 60\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SCENARIOS_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="duplicate scenario id"):
        scenarios_module.reload_scenarios()
    monkeypatch.delenv("SCENARIOS_ROOT")
    scenarios_module.reload_scenarios()
