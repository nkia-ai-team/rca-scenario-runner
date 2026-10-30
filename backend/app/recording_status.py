"""시나리오 카드의 녹화 상태: 지금 이 시나리오를 돌려도 되는가.

판정은 testbed-services 가 소유한 두 정본에서만 읽는다
(docs/spec-scenario-lifecycle.md §1, §3).

- manifest 의 ``stage``: ``official`` 이면 정상 녹화가 1개 이상 있고, ``candidate`` 면
  실행은 되지만 정상 녹화가 아직 없다.
- 녹화 대기 큐 ``recording-queue.json``: 항목의 ``status: hold`` 는 사람 확인이
  필요하다는 뜻이고 ``hold_reason`` 에 무엇을 확인해야 하는지 적혀 있다.

큐 파일은 러너가 이미 읽는 계약 root(``SCENARIO_CONTRACT_HOST_PATH`` →
``/opt/lucida/scenario-contracts``) 안에 있어 따로 배포할 것이 없다.

화면 표시용이다. 실행 허용 여부는 여기서 정하지 않는다(실행은
``ScenarioRunner.start`` 가 manifest 로 판단한다). 그래서 큐 파일이 없거나 깨져도
목록은 그대로 나가고 보류 표시만 빠진다.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel

DEFAULT_RECORDING_QUEUE_PATH = Path("/opt/lucida/scenario-contracts/recording-queue.json")

# 사유 한 줄의 상한. hold_reason 첫 문장이 이보다 길면 자른다(전문은 detail 에 있다).
_REASON_MAX = 160
_OPEN = "(（["
_CLOSE = ")）]"

RecordingLevel = Literal["hold", "unverified", "recorded"]


class RecordingStatus(BaseModel):
    """카드에 붙는 녹화 상태.

    - ``hold``: 녹화 대기 큐에서 보류. 사람 결정 전에는 돌리지 않는다.
    - ``unverified``: 후보. 정상 녹화가 없어 주입 결과가 검증되지 않았다.
    - ``recorded``: 정식. 정상 녹화가 1개 이상 있다.
    """

    level: RecordingLevel
    recommended: bool
    reason: str
    detail: Optional[str] = None


def recording_queue_path() -> Path:
    return Path(os.environ.get("RECORDING_QUEUE_PATH") or DEFAULT_RECORDING_QUEUE_PATH)


def load_recording_queue(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """scenario_id → 큐 항목. 파일이 없거나 모양이 어긋나면 빈 사전."""
    path = path or recording_queue_path()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = document.get("entries") if isinstance(document, dict) else None
    if not isinstance(entries, list):
        return {}
    queue: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        scenario_id = entry.get("scenario_id")
        if isinstance(scenario_id, str) and scenario_id and scenario_id not in queue:
            queue[scenario_id] = entry
    return queue


def first_sentence(text: str) -> str:
    """괄호 밖에서 처음 끝나는 문장. 마침표 뒤가 공백이나 끝일 때만 문장 끝으로 본다
    ("zstd -12", "0.8~0.93" 처럼 문장 안의 점은 건너뛴다)."""
    text = " ".join(text.split())
    depth = 0
    for index, char in enumerate(text):
        if char in _OPEN:
            depth += 1
        elif char in _CLOSE:
            depth = max(0, depth - 1)
        elif char == "." and depth == 0:
            if index + 1 == len(text) or text[index + 1] == " ":
                text = text[:index]
                break
    text = text.strip()
    if len(text) > _REASON_MAX:
        text = text[: _REASON_MAX - 1].rstrip() + "…"
    return text


def _text(entry: dict[str, Any] | None, key: str) -> str:
    value = (entry or {}).get(key)
    return value.strip() if isinstance(value, str) else ""


def judge_recording(stage: str | None, entry: dict[str, Any] | None) -> RecordingStatus | None:
    """stage 와 큐 항목으로 녹화 상태를 정한다. 둘 다 없으면(옛 catalog) None."""
    if _text(entry, "status") == "hold":
        hold_reason = _text(entry, "hold_reason")
        return RecordingStatus(
            level="hold",
            recommended=False,
            reason=first_sentence(hold_reason) or "녹화 대기 큐에서 보류됨",
            detail=hold_reason or None,
        )
    note = _text(entry, "note") or None
    if stage == "candidate":
        return RecordingStatus(
            level="unverified",
            recommended=False,
            reason="정상 녹화가 아직 없어 주입 결과가 검증되지 않았다",
            detail=note,
        )
    if stage == "official":
        return RecordingStatus(
            level="recorded",
            recommended=True,
            reason="정상 녹화가 1개 이상 있다",
            detail=f"재녹화 대기: {note}" if entry is not None and note else None,
        )
    return None
