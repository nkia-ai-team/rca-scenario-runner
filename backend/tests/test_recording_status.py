from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.recording_status import first_sentence, judge_recording, load_recording_queue


def write_queue(path: Path, entries: list[dict]) -> Path:
    path.write_text(
        json.dumps({"schema_version": 1, "entries": entries}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def entry(scenario_id: str, **overrides) -> dict:
    base = {
        "scenario_id": scenario_id,
        "reason": "new",
        "note": "새 시나리오 첫 검증",
        "requested_by": "testbed-scenario-create",
        "requested_at": "2026-10-10",
        "attempts": 0,
        "status": "waiting",
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    ("stage", "queued", "level", "recommended"),
    [
        # 보류는 stage 와 상관없이 가장 먼저 보인다(정식의 재녹화가 보류될 수도 있다).
        ("candidate", entry("X", status="hold", hold_reason="노드가 무너짐. 다시 돌리지 않는다."), "hold", False),
        ("official", entry("X", status="hold", hold_reason="재녹화 보류."), "hold", False),
        ("candidate", entry("X"), "unverified", False),
        ("candidate", None, "unverified", False),
        ("official", None, "recorded", True),
        ("official", entry("X", reason="recapture", note="제품 2026.10"), "recorded", True),
    ],
)
def test_judge_recording_levels(stage, queued, level, recommended) -> None:
    status = judge_recording(stage, queued)

    assert status is not None
    assert (status.level, status.recommended) == (level, recommended)
    assert status.reason


def test_hold_shows_first_sentence_and_keeps_full_reason() -> None:
    reason = (
        "주입이 노드 전체 붕괴로 번짐(설계 시트 §9 의 '노드 붕괴 위험'이 실제로 일어남). "
        "run 이 zstd -12 --long=31 두 흐름 주입 뒤 5xx 0.8~0.93 으로 피해가 났다. 사람 결정 필요."
    )

    status = judge_recording("candidate", entry("X", status="hold", hold_reason=reason))

    assert status is not None
    assert status.reason == "주입이 노드 전체 붕괴로 번짐(설계 시트 §9 의 '노드 붕괴 위험'이 실제로 일어남)"
    assert status.detail == reason


def test_hold_without_reason_still_says_why() -> None:
    status = judge_recording("candidate", entry("X", status="hold"))

    assert status is not None
    assert status.reason == "녹화 대기 큐에서 보류됨"
    assert status.detail is None


def test_recapture_note_reaches_detail_and_plain_official_has_none() -> None:
    queued = judge_recording("official", entry("X", reason="recapture", note="제품 2026.10"))
    plain = judge_recording("official", None)

    assert queued is not None and queued.detail == "재녹화 대기: 제품 2026.10"
    assert plain is not None and plain.detail is None


def test_legacy_without_stage_or_queue_has_no_status() -> None:
    assert judge_recording(None, None) is None
    assert judge_recording(None, entry("X")) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("짧은 사유", "짧은 사유"),
        ("첫 문장. 둘째 문장.", "첫 문장"),
        ("값 0.8~0.93 이 났다. 다음.", "값 0.8~0.93 이 났다"),
        ("괄호(안. 의 점) 은 넘긴다. 다음.", "괄호(안. 의 점) 은 넘긴다"),
        ("줄\n바꿈도   하나로. 다음", "줄 바꿈도 하나로"),
        ("가" * 200, "가" * 159 + "…"),
    ],
)
def test_first_sentence(text, expected) -> None:
    assert first_sentence(text) == expected


def test_load_queue_keys_by_scenario_and_keeps_first_duplicate(tmp_path) -> None:
    path = write_queue(
        tmp_path / "recording-queue.json",
        [entry("F01-R", status="hold"), entry("F01-R"), entry("F02-H"), {"status": "hold"}, "x"],
    )

    queue = load_recording_queue(path)

    assert set(queue) == {"F01-R", "F02-H"}
    assert queue["F01-R"]["status"] == "hold"


@pytest.mark.parametrize("content", [None, "not json", "[]", '{"entries": {}}'])
def test_unreadable_queue_is_empty(tmp_path, content) -> None:
    path = tmp_path / "recording-queue.json"
    if content is not None:
        path.write_text(content, encoding="utf-8")

    assert load_recording_queue(path) == {}


def test_queue_path_follows_env(tmp_path, monkeypatch) -> None:
    path = write_queue(tmp_path / "q.json", [entry("F09-P", status="hold")])
    monkeypatch.setenv("RECORDING_QUEUE_PATH", str(path))

    assert set(load_recording_queue()) == {"F09-P"}
