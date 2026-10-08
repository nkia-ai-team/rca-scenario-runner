from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import AsyncClient

from app.live_catalog import domains_of, list_live_scenarios
from app.runner import get_runner
from tests.test_external_live_manifests import manifest


def write_manifest(
    root: Path, scenario_id: str, *, live: bool = True, stage: str | None = None
) -> None:
    document = manifest("evaluation", live=live).model_dump()
    document["id"] = scenario_id
    if stage is not None:
        document["stage"] = stage
    document["slug"] = scenario_id.lower()
    if not live:
        document["readiness"] = "parked"
    root.mkdir(exist_ok=True)
    (root / f"{scenario_id.lower()}.json").write_text(json.dumps(document), encoding="utf-8")


def write_registry(path: Path, scenarios: dict[str, dict]) -> Path:
    path.write_text(
        json.dumps({"schema_version": "1.0", "scenarios": scenarios}), encoding="utf-8"
    )
    return path


def metadata(**overrides) -> dict:
    base = {
        "title": "English title",
        "description": "English description.",
        "cause": "fault injected -> queue stalls -> orders fail",
        "injection_summary": "Injection summary.",
        "user_impact": "User impact.",
        "distinguishing_evidence": "Lag **does not** rise.",
        "domain": "commerce",
    }
    base.update(overrides)
    return base


@pytest.fixture
def manifest_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "manifests"
    root.mkdir()
    monkeypatch.setenv("SCENARIO_MANIFEST_ROOT", str(root))
    return root


def test_live_manifest_becomes_a_card_with_registry_text(tmp_path, manifest_root) -> None:
    write_manifest(manifest_root, "F04-H")
    registry = write_registry(
        tmp_path / "scenario-metadata.json",
        {
            "F04-H": metadata(
                title_ko="주문 outbox relay 정지",
                cause_ko="relay 끔 → 미발행 행 증가 → 배송 생성 중단",
                difficulty="high",
                observation_domains=["DPM", "KCM"],
                must_support=["미발행 outbox 행 단조 증가"],
                must_rule_out=["컨슈머 정지가 아님"],
                scoring={"accept": ["commerce-order"], "partial": ["commerce-shipping"]},
            )
        },
    )

    [scenario] = list_live_scenarios(registry)

    assert scenario.id == scenario.short_id == "F04-H"
    assert (scenario.domain, scenario.domain_label) == ("commerce", "Commerce")
    assert scenario.name == "주문 outbox relay 정지"
    assert scenario.description == "English description."
    assert scenario.cause == "relay 끔"
    assert scenario.propagation_steps == ["relay 끔", "미발행 행 증가", "배송 생성 중단"]
    assert scenario.expected_alarms == ["미발행 outbox 행 단조 증가"]
    assert scenario.difficulty == 4
    assert scenario.cause_domain == "DPM, KCM"
    assert scenario.estimated_duration_sec == 10
    assert scenario.controller is not None
    assert scenario.expected_rca_root_cause == (
        "Lag does not rise.\n\n"
        "배제할 것:\n- 컨슈머 정지가 아님\n\n"
        "정답 대상: commerce-order\n\n"
        "부분 점수 대상: commerce-shipping"
    )


def test_manifest_stage_reaches_the_card(tmp_path, manifest_root) -> None:
    """testbed-services 수명주기: 후보(candidate)는 웹이 "검증 대기"로 따로 보인다.
    stage 가 없는 manifest(옛 catalog)는 None 이고 웹은 정식으로 취급한다."""
    write_manifest(manifest_root, "F01-R", stage="official")
    write_manifest(manifest_root, "F30-R", stage="candidate")
    write_manifest(manifest_root, "F04-H")
    registry = write_registry(
        tmp_path / "scenario-metadata.json",
        {sid: metadata() for sid in ("F01-R", "F30-R", "F04-H")},
    )

    stages = {scenario.id: scenario.stage for scenario in list_live_scenarios(registry)}

    assert stages == {"F01-R": "official", "F30-R": "candidate", "F04-H": None}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("easy", 2), ("medium", 3), ("중", 3), ("high", 4), ("HIGH", 4), (None, None), ("?", None)],
)
def test_registry_difficulty_word_maps_to_stars(tmp_path, manifest_root, raw, expected) -> None:
    write_manifest(manifest_root, "F01-R")
    registry = write_registry(
        tmp_path / "scenario-metadata.json", {"F01-R": metadata(difficulty=raw)}
    )

    [scenario] = list_live_scenarios(registry)

    assert scenario.difficulty == expected


def test_only_runnable_manifests_are_listed_in_id_order(tmp_path, manifest_root) -> None:
    for scenario_id in ("F10-H", "F02-P", "F02-H", "F03-R"):
        write_manifest(manifest_root, scenario_id)
    write_manifest(manifest_root, "F05-G", live=False)
    registry = write_registry(
        tmp_path / "scenario-metadata.json",
        {
            "F10-H": metadata(domain="food-delivery"),
            "F02-P": metadata(domain="core-banking"),
            "F02-H": metadata(),
            "F05-G": metadata(),
            # F03-R has no registry entry: the runner would refuse it.
        },
    )

    scenarios = list_live_scenarios(registry)

    assert [s.id for s in scenarios] == ["F02-H", "F02-P", "F10-H"]
    assert [(d.slug, d.label, d.scenario_count) for d in domains_of(scenarios)] == [
        ("commerce", "Commerce", 1),
        ("core-banking", "Core Banking", 1),
        ("food-delivery", "Food Delivery", 1),
    ]


@pytest.mark.parametrize("content", [None, "not json", "[]", '{"scenarios": []}'])
def test_unreadable_registry_lists_nothing(tmp_path, manifest_root, content) -> None:
    write_manifest(manifest_root, "F04-H")
    registry = tmp_path / "scenario-metadata.json"
    if content is not None:
        registry.write_text(content, encoding="utf-8")

    assert list_live_scenarios(registry) == []


async def test_api_lists_live_catalog_and_keeps_legacy_ids_resolvable(
    client: AsyncClient, tmp_path, manifest_root
) -> None:
    write_manifest(manifest_root, "F04-H")
    get_runner().scenario_metadata_path = write_registry(
        tmp_path / "scenario-metadata.json", {"F04-H": metadata(domain="core-banking")}
    )

    listed = (await client.get("/api/scenarios")).json()
    domains = (await client.get("/api/domains")).json()
    single = await client.get("/api/scenarios/F04-H")
    legacy = await client.get("/api/scenarios/01")

    assert [s["id"] for s in listed] == ["F04-H"]
    assert domains == [{"slug": "core-banking", "label": "Core Banking", "scenario_count": 1}]
    assert single.status_code == 200 and single.json()["id"] == "F04-H"
    assert legacy.status_code == 200 and legacy.json()["id"] == "plopvape-shop:01"


async def test_api_falls_back_to_legacy_catalog_without_live_manifests(
    client: AsyncClient,
) -> None:
    listed = (await client.get("/api/scenarios")).json()

    assert "plopvape-shop:01" in {s["id"] for s in listed}
