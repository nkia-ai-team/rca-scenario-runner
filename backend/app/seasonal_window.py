"""21일 창 오염 칸 계산 (testbed v4 G2META, questions.md Q19 덧붙임 답 A, B).

왜 있나
    stream 이상 탐지(observer)는 부팅할 때 168칸 버킷(요일 x 시각)을 VictoriaMetrics 이력
    21일로 다시 채운다(lucida-next develop-ai streamanomaly runner/main.go seasonalBackfillAt,
    STREAM_ANOMALY_SEASONAL_BACKFILL_DAYS 기본 21). 칸 번호는 service/detector.go bucketIndex 의
    ``weekday(일요일 0) * 24 + hour`` 이고 파이프라인 시각이 UTC 라 UTC 기준이다. 골든 복원은
    observer 를 다시 띄우므로 녹화마다 이 창이 다르다. 창 안에 앞선 주입이나 관측 정지가 있으면
    그 칸의 버킷이 장애 값이나 끊긴 값으로 학습된다. 이 모듈은 그 칸을 기계로 계산한다.
    녹화 의미는 바꾸지 않는다: 겹쳐도 채점에서 빼지 않고 기록만 한다.

    단 이 칸이 판정에 닿는지는 밴드 전략과 trainer 주기 프로필에 달렸다(Q19 덧붙임 2). observer
    기본 전략 auto_single 은 168칸 버킷을 쌓지도 쓰지도 않고(detector.go accumulateBucket 이 바로
    돌아감, 21일 백필도 버킷에 아무것도 쓰지 않음), 주간 기준선을 trainer 주기 프로필에서만 고른다.
    그래서 auto_single 이고 복원한 세대의 주기 프로필이 0 이며 시리즈별 전략 덮어쓰기가 없으면
    칸 겹침은 "판정 무관"이다(``judgement``). 그 밖(legacy_envelope, 주기 프로필 있음, 모름)이면
    원래 뜻의 "오염 칸 겹침"으로 쓴다.

무엇을 읽나
    - 주입: 러너 실행 기록(runs 폴더의 실행마다 result.json, state.json, timeline.json,
      cleanup.json). 구간 규칙은 testbed-scenario-verify 스킬 run_windows.py 의 겹침 규칙과 같다.
      시작은 result.json t1, 없으면 level_changes 의 가장 이른 applied_at. 끝은 result.json t2,
      cleanup.json effect_ended_at(러너 모양과 수동 정리의 평평한 모양 둘 다), level_changes
      effect_ended_at 중 가장 늦은 것. 끝 기록이 없으면 창 끝까지 효과가 남은 것으로 본다.
      러너로 돌린 시연 수동 실행도 같은 runs 폴더에 남으므로 센다. 러너 밖에서 손으로 낸 장애는
      기록이 없어 세지 못한다(limits 에 적는다).
    - 정지: 관측 정지 기록 파일(testbed-services scripts/scenarios/registry/observation-outages.json,
      러너 컨테이너 안 /opt/lucida/scenario-contracts/registry/observation-outages.json). 칸 목록을
      코드에 두지 않고 기록마다 출처를 둔다. 정지 칸은 정지 앞뒤다: 기록의 start 부터 1시간(정지로
      가는 저하 구간)과 end 부터 1시간(재기동과 밀린 수집). 정지 가운데는 표본이 없어 값이 오염되지
      않고 그 칸의 주 수만 준다.

명령 (표준 라이브러리만 쓴다. 러너 호스트에서 표준 입력으로 흘려 돌릴 수 있다)
    python3 seasonal_window.py window --runs-root DIR [--outages FILE | --outages-json JSON]
        [--at ISO_UTC] [--days 21]
    python3 seasonal_window.py plan --runs-root DIR [--outages FILE | --outages-json JSON]
        [--from ISO_UTC] [--hours 48] [--lead-min 130] [--days 21]
        [--band-strategy S --periodic-count N]

    plan 머리에는 판정 관련 여부를 찍는다: 인자로 준 밴드 전략과 주기 프로필 수, 없으면 runs 폴더의
    가장 최근 phases.json trainer_reset 기록(러너가 복원 때 적은 값).

    plan 은 지금부터 N시간 안의 시각 칸마다, 그 시각에 주입을 시작하면(복원은 lead 분 앞) 그
    녹화의 21일 창에서 그 칸과 다음 칸이 깨끗한지를 KST 로 낸다. 러너 호스트(tb-ops)에서:

    ssh -J root@192.168.200.66 nkia@192.168.122.20 \\
      "sudo -n python3 - plan --runs-root /root/rca-scenario-runner/runs \\
       --outages-json '$(jq -c . observation-outages.json)'" < seasonal_window.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_DAYS = 21
DEFAULT_OUTAGES_PATH = Path("/opt/lucida/scenario-contracts/registry/observation-outages.json")
# Normal 2h + buffer 10m: the injection starts this long after the golden restore.
DEFAULT_LEAD = timedelta(minutes=130)
# observer code default (lucida-next develop-ai streamanomaly service/params.go, 2026-08-03 전환).
DEFAULT_BAND_STRATEGY = os.environ.get("OBSERVER_DEFAULT_BAND_STRATEGY", "auto_single")
AUTO_SINGLE = "auto_single"
OUTAGE_EDGE = timedelta(hours=1)
HOUR = timedelta(hours=1)
KST = timezone(timedelta(hours=9))
WEEKDAY_KO = ("일", "월", "화", "수", "목", "금", "토")
LIMITS = (
    "주입은 러너 runs 폴더에 남은 실행만 센다(러너로 돌린 시연 수동 실행 포함). 러너 밖에서 손으로 낸 장애, "
    "지워진 실행 폴더는 기록이 없어 세지 못한다.",
    "정지는 관측 정지 기록 파일에 적힌 것만 센다. 칸은 정지 앞뒤 각 1시간이고, 정지 가운데 칸은 표본이 "
    "빠질 뿐 값이 오염되지 않아 세지 않는다.",
    "녹화가 쌓일수록 21일 창 안 칸 대부분이 겹치게 되어 이 표시로 가르는 힘이 약해진다.",
)


def parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def fmt(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def cell_of(moment: datetime) -> int:
    """observer bucketIndex: weekday (Sunday 0) * 24 + hour, in UTC."""
    moment = moment.astimezone(timezone.utc)
    return ((moment.weekday() + 1) % 7) * 24 + moment.hour


def kst_label(cell: int) -> str:
    """Human label of a UTC hour-of-week cell, read in KST (the same 168 cells, shifted 9h)."""
    kst = (cell + 9) % 168
    return f"{WEEKDAY_KO[kst // 24]} {kst % 24:02d}시"


def _floor_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def hour_cells(start: datetime, end: datetime) -> list[int]:
    """Cells of every hour that [start, end) touches. A zero-length span still
    touches the hour it sits in; an inverted span touches nothing."""
    if end < start:
        return []
    hour = _floor_hour(start)
    cells = [cell_of(hour)]
    hour += HOUR
    while hour < end:
        cells.append(cell_of(hour))
        hour += HOUR
    return sorted(set(cells))


def _clip(start: datetime, end: datetime, lo: datetime, hi: datetime) -> tuple[datetime, datetime] | None:
    s, e = max(start, lo), min(end, hi)
    return (s, e) if s < e or (s == e and lo <= s < hi) else None


# ---------------------------------------------------------------- injections


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _cleanup_end(cleanup: Any) -> datetime | None:
    """cleanup.json has two shapes: the runner's {"cleanup": {...}} and a manual
    cleanup's flat {...}."""
    if not isinstance(cleanup, dict):
        return None
    inner = cleanup.get("cleanup") if isinstance(cleanup.get("cleanup"), dict) else cleanup
    return parse_utc(inner.get("effect_ended_at"))


def run_window(run_id: str, result: Any = None, state: Any = None, timeline: Any = None,
               cleanup: Any = None) -> dict[str, Any] | None:
    """One run's effect span (verify skill run_windows.py rule). None when it applied nothing."""
    result = result if isinstance(result, dict) else {}
    state = state if isinstance(state, dict) else {}
    timeline = timeline if isinstance(timeline, dict) else {}
    changes = state.get("level_changes") or timeline.get("level_changes") or []
    changes = [c for c in changes if isinstance(c, dict)]
    t1 = parse_utc(result.get("t1"))
    applied = [t for t in (parse_utc(c.get("applied_at")) for c in changes) if t]
    if t1 is None and applied:
        t1 = min(applied)
    if t1 is None:
        return None
    ends = [parse_utc(result.get("t2")), _cleanup_end(cleanup)]
    ends += [parse_utc(c.get("effect_ended_at")) for c in changes]
    ends = [t for t in ends if t]
    end = max(ends) if ends else None
    return {
        "run_id": run_id,
        "scenario_id": result.get("scenario_id") or state.get("scenario_id"),
        "t1": fmt(t1),
        "end": fmt(end),
        "basis": "result.json" if result else ("state.json" if state else "timeline.json"),
    }


def scan_runs(runs_root: Path, exclude: str | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """Effect spans of every run under runs_root, plus run dirs that could not be read."""
    windows: list[dict[str, Any]] = []
    unreadable: list[str] = []
    try:
        entries = sorted(runs_root.iterdir())
    except OSError:
        return windows, [str(runs_root)]
    for run_dir in entries:
        name = run_dir.name
        # Hidden capsule staging dirs are not runs. Other non-runs (cycle-topology/)
        # carry none of the four records below and fall out on their own.
        if name == exclude or name.startswith(".") or not run_dir.is_dir():
            continue
        docs = {k: _load(run_dir / f"{k}.json") for k in ("result", "state", "timeline", "cleanup")}
        # A record that exists but does not parse may hide an injection: name it.
        # A run with none of the four (plan or lease only) applied nothing.
        if any((run_dir / f"{k}.json").is_file() and not isinstance(v, dict) for k, v in docs.items()):
            unreadable.append(name)
        window = run_window(name, **docs)
        if window is not None:
            windows.append(window)
    return windows, unreadable


# ------------------------------------------------------------------ outages


def load_outages(path: Path | None = None, *, text: str | None = None) -> tuple[list[dict[str, Any]], str]:
    """Outage records and a status ("ok", "missing", "invalid: ...").

    The file is a ledger, not a list of cells: every record names its span and
    where that span comes from. A malformed file is refused whole so a typo never
    silently drops an outage."""
    if text is None:
        if path is None or not path.is_file():
            return [], "missing"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            return [], f"invalid: {error}"
    try:
        document = json.loads(text)
    except ValueError as error:
        return [], f"invalid: not JSON ({error})"
    records = document.get("outages") if isinstance(document, dict) else None
    if not isinstance(records, list):
        return [], "invalid: outages must be a list"
    out = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            return [], f"invalid: outages[{index}] is not an object"
        start, end = parse_utc(record.get("start")), parse_utc(record.get("end"))
        source = record.get("source")
        ident = record.get("id")
        if not isinstance(ident, str) or not ident:
            return [], f"invalid: outages[{index}].id"
        if start is None or end is None or end < start:
            return [], f"invalid: outages[{index}] start/end"
        if not isinstance(source, str) or not source.strip():
            return [], f"invalid: outages[{index}].source"
        out.append({"id": ident, "start": fmt(start), "end": fmt(end), "source": source})
    return out, "ok"


def outage_edges(record: dict[str, Any]) -> list[tuple[datetime, datetime]]:
    start, end = parse_utc(record["start"]), parse_utc(record["end"])
    return [(start, start + OUTAGE_EDGE), (end, end + OUTAGE_EDGE)]


# ------------------------------------------------------------------- window


def _cell_rows(hits: dict[int, set[str]], key: str) -> list[dict[str, Any]]:
    return [{"cell": c, "kst": kst_label(c), key: sorted(hits[c])} for c in sorted(hits)]


def window_report(
    restored_at: datetime,
    *,
    days: int,
    windows: list[dict[str, Any]],
    outages: list[dict[str, Any]],
    sources: dict[str, Any],
) -> dict[str, Any]:
    """Injection and outage cells inside [restored_at - days, restored_at)."""
    hi = restored_at.astimezone(timezone.utc)
    lo = hi - timedelta(days=days)
    injection_hits: dict[int, set[str]] = {}
    runs_counted = []
    for window in windows:
        start = parse_utc(window["t1"])
        end = parse_utc(window.get("end"))
        open_end = end is None
        clipped = _clip(start, end or hi, lo, hi)
        if clipped is None:
            continue
        runs_counted.append({**window, "open_end": open_end})
        for cell in hour_cells(*clipped):
            injection_hits.setdefault(cell, set()).add(window["run_id"])
    outage_hits: dict[int, set[str]] = {}
    outages_counted = []
    for record in outages:
        touched = False
        for edge in outage_edges(record):
            clipped = _clip(*edge, lo, hi)
            if clipped is None:
                continue
            touched = True
            for cell in hour_cells(*clipped):
                outage_hits.setdefault(cell, set()).add(record["id"])
        if touched:
            outages_counted.append(record)
    limits = list(LIMITS)
    if sources.get("outages_status") != "ok":
        limits.append(f"관측 정지 기록을 읽지 못했다({sources.get('outages_status')}): 정지 칸이 빠졌을 수 있다.")
    if sources.get("unreadable_runs"):
        limits.append("읽지 못한 실행 폴더가 있다: " + ", ".join(sources["unreadable_runs"]))
    open_runs = [r["run_id"] for r in runs_counted if r["open_end"]]
    if open_runs:
        limits.append("끝 기록이 없어 창 끝까지 효과로 센 실행: " + ", ".join(open_runs))
    return {
        "status": "ok",
        "cell_unit": "utc_hour_of_week",
        "days": days,
        "start": fmt(lo),
        "end": fmt(hi),
        "injection_cells": _cell_rows(injection_hits, "runs"),
        "outage_cells": _cell_rows(outage_hits, "outages"),
        "runs_counted": runs_counted,
        "outages_counted": outages_counted,
        "sources": sources,
        "limits": limits,
    }


def contaminated_cells(report: dict[str, Any]) -> set[int]:
    return {row["cell"] for row in report.get("injection_cells", [])} | {
        row["cell"] for row in report.get("outage_cells", [])
    }


def with_overlap(report: dict[str, Any], t1: datetime, t2: datetime) -> dict[str, Any]:
    """Add this recording's injection cells and their overlap with the window's cells."""
    mine = hour_cells(t1, t2)
    dirty = contaminated_cells(report)
    overlap = [c for c in mine if c in dirty]
    return {
        **report,
        "this_injection_cells": [{"cell": c, "kst": kst_label(c)} for c in mine],
        "overlap_cells": [{"cell": c, "kst": kst_label(c)} for c in overlap],
        "overlap": bool(overlap),
    }


def compute(
    restored_at: datetime,
    *,
    runs_root: Path,
    outages_path: Path | None,
    days: int,
    exclude_run_id: str | None = None,
    outages_text: str | None = None,
) -> dict[str, Any]:
    windows, unreadable = scan_runs(runs_root, exclude=exclude_run_id)
    outages, status = load_outages(outages_path, text=outages_text)
    sources = {
        "runs_root": str(runs_root),
        "outages_path": "inline" if outages_text is not None else str(outages_path),
        "outages_status": status,
        "unreadable_runs": unreadable,
    }
    return window_report(restored_at, days=days, windows=windows, outages=outages, sources=sources)


# ---------------------------------------------------------------- judgement


def band_strategy(sources: Any, default: str = DEFAULT_BAND_STRATEGY) -> dict[str, Any]:
    """The observer's effective global band strategy from what the restore read.

    Precedence as the observer applies it at boot (runner/main.go, runtime.go
    effectiveParams, model.go bootLoadModel): the stream-anomaly detection policy
    params over the boot-loaded model artifact's params over the code default. The
    artifact is merged over the default, and refused (params kept) when its sha256
    differs from the version checksum. Sources the restore could not read stay
    unknown rather than falling back to the default."""
    if not isinstance(sources, dict):
        return {"value": None, "source": "unknown", "coverage_overrides": None}
    overrides = sources.get("coverage_overrides")
    overrides = overrides if isinstance(overrides, int) and not isinstance(overrides, bool) else None
    policy, binding = sources.get("detection_policy"), sources.get("model_binding")
    artifact_band = sources.get("artifact_band_strategy")
    if isinstance(policy, str) and policy:
        value, source = policy, "detection_policy"
    elif binding is False:
        value, source = default, "code_default"
    elif binding is not True:
        value, source = None, "unknown"
    elif sources.get("artifact_read") is not True:
        value, source = None, "model_artifact_unread"
    elif sources.get("artifact_checksum_ok") is False:
        value, source = default, "code_default_artifact_refused"
    elif sources.get("artifact_checksum_ok") is not True:
        value, source = None, "model_artifact_unread"
    elif isinstance(artifact_band, str) and artifact_band:
        value, source = artifact_band, "model_artifact"
    else:
        value, source = default, "code_default_artifact_silent"
    return {"value": value, "source": source, "coverage_overrides": overrides}


def judgement(band: dict[str, Any] | None, periodic_count: Any) -> dict[str, Any]:
    """Whether the 21-day bucket window can reach the observer's judgement.

    relevant False only when it provably cannot: auto_single (no weekly buckets),
    zero trainer periodic profiles (no other weekly baseline) and no series that
    overrides the strategy. Anything unknown stays relevant=None (treated as relevant)."""
    band = band or {}
    value, overrides = band.get("value"), band.get("coverage_overrides")
    known_count = isinstance(periodic_count, int) and not isinstance(periodic_count, bool)
    if value == AUTO_SINGLE and known_count and periodic_count == 0 and overrides == 0:
        return {"relevant": False, "reason": "주기 프로필 0, auto_single"}
    if value is None or not known_count or overrides is None:
        missing = [name for name, ok in (("밴드 전략", value is not None), ("주기 프로필 수", known_count),
                                         ("시리즈별 덮어쓰기 수", overrides is not None)) if not ok]
        return {"relevant": None, "reason": "모름(" + ", ".join(missing) + ")"}
    parts = []
    if value != AUTO_SINGLE:
        parts.append(f"밴드 {value}")
    if periodic_count:
        parts.append(f"주기 프로필 {periodic_count}")
    if overrides:
        parts.append(f"시리즈별 덮어쓰기 {overrides}")
    return {"relevant": True, "reason": ", ".join(parts)}


def latest_recorded_judgement(runs_root: Path) -> dict[str, Any] | None:
    """The newest trainer_reset in runs_root/*/phases.json that carries the band record."""
    best: tuple[datetime, dict[str, Any]] | None = None
    try:
        entries = list(runs_root.iterdir())
    except OSError:
        return None
    for run_dir in entries:
        doc = _load(run_dir / "phases.json") if run_dir.is_dir() else None
        phases = doc.get("phases") if isinstance(doc, dict) else None
        reset = next((p for p in phases or [] if isinstance(p, dict) and p.get("phase") == "trainer_reset"), None)
        if not reset or "observer_band_strategy" not in reset:
            continue
        at = parse_utc(reset.get("at"))
        if at is None or (best is not None and at <= best[0]):
            continue
        band = reset.get("observer_band_strategy") or {}
        best = (at, {"run_id": run_dir.name, "at": fmt(at), "band": band,
                     "periodic_count": reset.get("golden_stream_periodic_count"),
                     "judgement": judgement(band, reset.get("golden_stream_periodic_count"))})
    return best[1] if best else None


# --------------------------------------------------------------------- plan


def plan_rows(
    start: datetime,
    *,
    hours: int,
    lead: timedelta,
    days: int,
    windows: list[dict[str, Any]],
    outages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """For each hour from start: would an injection starting then sit on a dirty cell
    of its own 21-day window (restore = injection start - lead)?"""
    first = _floor_hour(start.astimezone(timezone.utc))
    if first < start:
        first += HOUR
    rows = []
    for i in range(hours):
        inject_at = first + i * HOUR
        report = window_report(
            inject_at - lead, days=days, windows=windows, outages=outages, sources={"outages_status": "ok"}
        )
        reasons = {row["cell"]: row["runs"] for row in report["injection_cells"]}
        for row in report["outage_cells"]:
            reasons.setdefault(row["cell"], [])
            reasons[row["cell"]] = reasons[row["cell"]] + row["outages"]
        cell, nxt = cell_of(inject_at), cell_of(inject_at + HOUR)
        rows.append({
            "inject_at": fmt(inject_at),
            "inject_at_kst": inject_at.astimezone(KST).strftime("%m-%d %H:%M"),
            "cell": cell,
            "kst": kst_label(cell),
            "clean": cell not in reasons,
            "next_clean": nxt not in reasons,
            "why": sorted(reasons.get(cell, [])),
            "next_why": sorted(reasons.get(nxt, [])),
        })
    return rows


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="seasonal_window")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("window", "plan"):
        p = sub.add_parser(name)
        p.add_argument("--runs-root", required=True, type=Path)
        group = p.add_mutually_exclusive_group()
        group.add_argument("--outages", type=Path, default=None)
        group.add_argument("--outages-json", default=None)
        p.add_argument("--days", type=int, default=int(os.environ.get(
            "STREAM_ANOMALY_SEASONAL_BACKFILL_DAYS", DEFAULT_DAYS)))
        if name == "window":
            p.add_argument("--at", default=None, help="restore time, ISO UTC (default now)")
        else:
            p.add_argument("--from", dest="start", default=None, help="ISO UTC (default now)")
            p.add_argument("--hours", type=int, default=48)
            p.add_argument("--lead-min", type=int, default=int(DEFAULT_LEAD.total_seconds() // 60))
            p.add_argument("--band-strategy", default=None, help="observer band strategy, overrides the record")
            p.add_argument("--periodic-count", type=int, default=None, help="active generation periodic profiles")
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    if args.command == "window":
        at = parse_utc(args.at) if args.at else now
        if at is None:
            parser.error("--at must be an ISO UTC time")
        report = compute(at, runs_root=args.runs_root, outages_path=args.outages, days=args.days,
                         outages_text=args.outages_json)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    start = parse_utc(args.start) if args.start else now
    if start is None:
        parser.error("--from must be an ISO UTC time")
    windows, unreadable = scan_runs(args.runs_root)
    outages, status = load_outages(args.outages, text=args.outages_json)
    rows = plan_rows(start, hours=args.hours, lead=timedelta(minutes=args.lead_min), days=args.days,
                     windows=windows, outages=outages)
    print(f"# 주입 시작 후보 {args.hours}시간 (KST, 복원은 주입 {args.lead_min}분 앞, 창 {args.days}일)")
    print(f"# 실행 {len(windows)}개, 읽지 못한 실행 폴더 {len(unreadable)}, 정지 기록 {status} {len(outages)}건")
    if args.band_strategy is not None or args.periodic_count is not None:
        band = {"value": args.band_strategy, "source": "argument", "coverage_overrides": 0}
        verdict = judgement(band, args.periodic_count)
        basis = f"인자(밴드 {args.band_strategy}, 주기 프로필 {args.periodic_count}, 시리즈별 덮어쓰기 0 가정)"
    else:
        record = latest_recorded_judgement(args.runs_root)
        if record is None:
            verdict = {"relevant": None, "reason": "복원 기록 없음"}
            basis = "runs 폴더에 밴드 기록이 있는 phases.json 이 없다(G2META 러너 배포 전). --band-strategy, --periodic-count 로 줄 수 있다"
        else:
            verdict = record["judgement"]
            band = record["band"]
            basis = (f"최근 복원 기록 {record['run_id']} {parse_utc(record['at']).astimezone(KST):%m-%d %H:%M} KST, "
                     f"밴드 {band.get('value')}({band.get('source')}), 주기 프로필 {record['periodic_count']}")
    if verdict["relevant"] is False:
        print(f"# 판정 관련: 무관({verdict['reason']}). 21일 창 칸은 판정에 닿지 않아 주입 시각 제약이 없다. 근거: {basis}")
    else:
        word = "관련" if verdict["relevant"] else "모름, 관련으로 본다"
        print(f"# 판정 관련: {word}({verdict['reason']}). 아래 깨끗한 칸에서 고른다. 근거: {basis}")
    print("주입 시작 KST | 칸 | 이 칸 | 다음 칸 | 오염 출처")
    for row in rows:
        mark = "깨끗" if row["clean"] else "오염"
        nmark = "깨끗" if row["next_clean"] else "오염"
        why = ", ".join(row["why"] + [f"다음 칸 {w}" for w in row["next_why"]]) or "-"
        print(f"{row['inject_at_kst']} | {row['kst']} | {mark} | {nmark} | {why}")
    clean = [r["inject_at_kst"] for r in rows if r["clean"] and r["next_clean"]]
    print(f"# 이 칸과 다음 칸이 모두 깨끗한 시작 시각 {len(clean)}개: {', '.join(clean) or '없음'}")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
