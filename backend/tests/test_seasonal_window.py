"""21-day seasonal window cells (testbed v4 G2META, Q19 A): app.seasonal_window.

The observer rebuilds its 168 stream buckets (UTC weekday x hour, Sunday 0) from
21 days of VM history when the golden restore reboots it. These tests pin the
cell arithmetic to that bucketIndex, the run-record rule, the outage ledger, the
window clipping, the overlap line and the stdlib-only CLI the command session
pipes to the runner host.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest

from app import seasonal_window as sw

MODULE = Path(sw.__file__)


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def write(run_dir: Path, name: str, doc) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / name).write_text(json.dumps(doc), encoding="utf-8")


# ------------------------------------------------------------------- cells


@pytest.mark.parametrize(
    ("moment", "cell"),
    [
        ("2026-11-15T00:00:00Z", 0),  # Sunday 00 UTC
        ("2026-11-14T23:59:59Z", 167),  # Saturday 23 UTC
        ("2026-11-12T16:47:09Z", 4 * 24 + 16),  # golden capture, Thursday 16 UTC
        ("2026-11-13T01:47:09+09:00", 4 * 24 + 16),  # same instant written in KST
    ],
)
def test_cell_is_the_observer_bucket_index_in_utc(moment, cell) -> None:
    assert sw.cell_of(utc(moment)) == cell


@pytest.mark.parametrize(
    ("cell", "label"),
    [(0, "일 09시"), (167, "일 08시"), (4 * 24 + 16, "금 01시"), (14, "일 23시"), (15, "월 00시")],
)
def test_kst_label_reads_the_utc_cell_in_kst(cell, label) -> None:
    assert sw.kst_label(cell) == label


def test_hour_cells_touch_every_hour_of_the_span() -> None:
    assert sw.hour_cells(utc("2026-11-12T10:10:00Z"), utc("2026-11-12T10:15:00Z")) == [106]
    assert sw.hour_cells(utc("2026-11-12T10:50:00Z"), utc("2026-11-12T12:05:00Z")) == [106, 107, 108]
    # An end exactly on the hour does not touch the next hour.
    assert sw.hour_cells(utc("2026-11-12T10:10:00Z"), utc("2026-11-12T11:00:00Z")) == [106]
    # Zero length still sits in its hour; inverted touches nothing.
    assert sw.hour_cells(utc("2026-11-12T10:10:00Z"), utc("2026-11-12T10:10:00Z")) == [106]
    assert sw.hour_cells(utc("2026-11-12T10:10:00Z"), utc("2026-11-12T10:09:00Z")) == []
    # Saturday 23 into Sunday 00 wraps the week.
    assert sw.hour_cells(utc("2026-11-14T23:30:00Z"), utc("2026-11-15T00:30:00Z")) == [0, 167]


# ------------------------------------------------------------- run records


def test_run_window_follows_the_verify_overlap_rule() -> None:
    # result.json t1/t2, and the runner-shaped cleanup end extends it.
    window = sw.run_window(
        "A-run-1",
        result={"scenario_id": "A", "t1": "2026-11-11T07:14:52Z", "t2": "2026-11-11T07:32:40Z"},
        cleanup={"cleanup": {"effect_ended_at": "2026-11-11T07:40:00Z"}},
    )
    assert window == {
        "run_id": "A-run-1", "scenario_id": "A", "t1": "2026-11-11T07:14:52Z",
        "end": "2026-11-11T07:40:00Z", "basis": "result.json",
    }
    # No result.json: earliest applied_at starts it; a manual (flat) cleanup ends it.
    window = sw.run_window(
        "B-run-1",
        state={"scenario_id": "B", "level_changes": [
            {"applied_at": "2026-10-23T16:50:00Z", "effect_ended_at": None},
            {"applied_at": "2026-10-23T16:41:16Z", "effect_ended_at": "2026-10-23T16:45:00Z"},
        ]},
        cleanup={"succeeded": True, "effect_ended_at": "2026-10-23T17:02:34Z"},
    )
    assert window["t1"] == "2026-10-23T16:41:16Z"
    assert window["end"] == "2026-10-23T17:02:34Z"
    assert window["basis"] == "state.json"
    # Applied but never ended: open end.
    window = sw.run_window("C-run-1", timeline={"level_changes": [{"applied_at": "2026-10-23T16:41:16Z"}]})
    assert window["end"] is None and window["basis"] == "timeline.json"
    # Nothing applied: not an injection.
    assert sw.run_window("D-run-1", state={"status": "clean", "level_changes": []}) is None


def test_scan_runs_reads_run_dirs_only(tmp_path: Path) -> None:
    write(tmp_path / "A-run-1", "result.json", {"t1": "2026-11-11T07:14:52Z", "t2": "2026-11-11T07:32:40Z"})
    write(tmp_path / "E-run-1", "result.json", {"t1": "2026-11-10T07:14:52Z", "t2": "2026-11-10T07:32:40Z"})
    (tmp_path / "cycle-topology" / "q-0").mkdir(parents=True)  # not a run: no marker
    # hidden staging dir, even with a record in it
    write(tmp_path / ".A-run-2.capsule.x", "result.json", {"t1": "2026-11-11T08:00:00Z", "t2": "2026-11-11T08:10:00Z"})
    (tmp_path / "Z-run-1").mkdir()
    (tmp_path / "Z-run-1" / "result.json").write_text("{not json", encoding="utf-8")
    write(tmp_path / "P-run-1", "plan.json", {})  # created, nothing applied
    windows, unreadable = sw.scan_runs(tmp_path, exclude="E-run-1")
    assert [w["run_id"] for w in windows] == ["A-run-1"]
    assert unreadable == ["Z-run-1"]


# ----------------------------------------------------------------- outages


def test_outage_ledger_status(tmp_path: Path) -> None:
    assert sw.load_outages(tmp_path / "absent.json") == ([], "missing")
    good = {"outages": [{"id": "o1", "start": "2026-10-30T16:33:00Z", "end": "2026-11-09T08:12:00Z",
                         "source": "board"}]}
    records, status = sw.load_outages(text=json.dumps(good))
    assert status == "ok" and records == good["outages"]
    for bad in (
        "{not json",
        json.dumps({"outages": {}}),
        json.dumps({"outages": [{**good["outages"][0], "end": "2026-10-30T16:00:00Z"}]}),
        json.dumps({"outages": [{**good["outages"][0], "source": " "}]}),
        json.dumps({"outages": [{**good["outages"][0], "id": ""}]}),
        json.dumps({"outages": [{**good["outages"][0], "start": "2026-10-30 16:33"}]}),
        json.dumps({"outages": [good["outages"][0], "x"]}),
    ):
        records, status = sw.load_outages(text=bad)
        assert records == [] and status.startswith("invalid"), bad


# ------------------------------------------------------------------ window

RESTORE = utc("2026-11-13T04:10:00Z")  # window [10-23 04:10, 11-13 04:10)


def _report(windows, outages=(), status="ok"):
    return sw.window_report(RESTORE, days=21, windows=list(windows), outages=list(outages),
                            sources={"outages_status": status, "unreadable_runs": []})


def test_window_counts_only_the_part_inside_the_21_days() -> None:
    report = _report([
        # straddles the window start: only 04:10..05:30 counts
        {"run_id": "S", "t1": "2026-10-23T03:00:00Z", "end": "2026-10-23T05:30:00Z"},
        # entirely before the window
        {"run_id": "O", "t1": "2026-10-22T09:00:00Z", "end": "2026-10-22T09:20:00Z"},
        # demo run inside
        {"run_id": "F51", "t1": "2026-11-11T07:14:52Z", "end": "2026-11-11T07:32:40Z"},
        # after the restore: not part of this window
        {"run_id": "L", "t1": "2026-11-13T05:00:00Z", "end": "2026-11-13T05:10:00Z"},
    ])
    assert report["start"] == "2026-10-23T04:10:00Z" and report["end"] == "2026-11-13T04:10:00Z"
    cells = {row["cell"]: row["runs"] for row in report["injection_cells"]}
    fri = 5 * 24
    wed = 3 * 24
    assert cells == {fri + 4: ["S"], fri + 5: ["S"], wed + 7: ["F51"]}
    assert [r["run_id"] for r in report["runs_counted"]] == ["S", "F51"]
    assert next(row for row in report["injection_cells"] if row["cell"] == wed + 7)["kst"] == "수 16시"


def test_open_ended_run_counts_to_the_window_end_and_is_named_in_limits() -> None:
    report = _report([{"run_id": "X", "t1": "2026-11-13T02:30:00Z", "end": None}])
    fri = 5 * 24
    assert [row["cell"] for row in report["injection_cells"]] == [fri + 2, fri + 3, fri + 4]
    assert report["runs_counted"][0]["open_end"] is True
    assert any("X" in line and "끝 기록" in line for line in report["limits"])


def test_outage_counts_its_edges_not_its_middle() -> None:
    outage = {"id": "119-stop", "start": "2026-10-30T16:33:00Z", "end": "2026-11-09T08:12:00Z", "source": "s"}
    report = _report([], [outage])
    fri, mon = 5 * 24, 1 * 24
    assert {row["cell"]: row["outages"] for row in report["outage_cells"]} == {
        fri + 16: ["119-stop"], fri + 17: ["119-stop"], mon + 8: ["119-stop"], mon + 9: ["119-stop"],
    }
    assert report["outages_counted"] == [outage]
    # An outage that ended before the window is not counted.
    old = {**outage, "id": "old", "start": "2026-09-01T00:00:00Z", "end": "2026-09-02T00:00:00Z"}
    assert _report([], [old])["outage_cells"] == [] and _report([], [old])["outages_counted"] == []


def test_missing_outage_ledger_is_a_stated_limit() -> None:
    report = _report([], [], status="missing")
    assert any("관측 정지 기록을 읽지 못했다(missing)" in line for line in report["limits"])
    assert any("녹화가 쌓일수록" in line for line in report["limits"])


def test_overlap_line_inputs() -> None:
    report = _report([{"run_id": "F51", "t1": "2026-11-11T07:14:52Z", "end": "2026-11-11T07:32:40Z"}])
    wed = 3 * 24
    hit = sw.with_overlap(report, utc("2026-11-18T07:50:00Z"), utc("2026-11-18T08:20:00Z"))
    assert [c["cell"] for c in hit["this_injection_cells"]] == [wed + 7, wed + 8]
    assert hit["overlap"] is True and hit["overlap_cells"] == [{"cell": wed + 7, "kst": "수 16시"}]
    miss = sw.with_overlap(report, utc("2026-11-18T08:10:00Z"), utc("2026-11-18T08:20:00Z"))
    assert miss["overlap"] is False and miss["overlap_cells"] == []
    # Outage cells count as contaminated too.
    outage = {"id": "o", "start": "2026-11-09T08:12:00Z", "end": "2026-11-09T08:12:00Z", "source": "s"}
    with_outage = _report([], [outage])
    assert sw.with_overlap(with_outage, utc("2026-11-16T08:30:00Z"), utc("2026-11-16T08:40:00Z"))["overlap"]


def test_compute_reads_runs_and_ledger(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    write(runs / "F51-H-run-1", "result.json", {"scenario_id": "F51-H", "t1": "2026-11-11T07:14:52Z",
                                                "t2": "2026-11-11T07:32:40Z"})
    ledger = tmp_path / "observation-outages.json"
    ledger.write_text(json.dumps({"outages": [
        {"id": "o", "start": "2026-11-09T08:12:00Z", "end": "2026-11-09T09:00:00Z", "source": "s"}]}),
        encoding="utf-8")
    report = sw.compute(RESTORE, runs_root=runs, outages_path=ledger, days=21)
    assert report["sources"] == {"runs_root": str(runs), "outages_path": str(ledger),
                                 "outages_status": "ok", "unreadable_runs": []}
    assert report["injection_cells"][0]["runs"] == ["F51-H-run-1"]
    assert report["outage_cells"]
    assert sw.compute(RESTORE, runs_root=runs, outages_path=ledger, days=1)["injection_cells"] == []


# -------------------------------------------------------------------- plan


def test_plan_marks_hours_whose_cell_was_injected_inside_their_own_window() -> None:
    runs = [{"run_id": "F51", "t1": "2026-11-11T07:14:52Z", "end": "2026-11-11T07:32:40Z"}]
    rows = sw.plan_rows(utc("2026-11-13T00:00:00Z"), hours=168, lead=timedelta(minutes=130), days=21,
                        windows=runs, outages=[])
    assert len(rows) == 168 and rows[0]["inject_at"] == "2026-11-13T00:00:00Z"
    dirty = [r for r in rows if not r["clean"]]
    assert [r["inject_at"] for r in dirty] == ["2026-11-18T07:00:00Z"]
    assert dirty[0]["why"] == ["F51"] and dirty[0]["inject_at_kst"] == "11-18 16:00"
    before = next(r for r in rows if r["inject_at"] == "2026-11-18T06:00:00Z")
    assert before["clean"] and not before["next_clean"] and before["next_why"] == ["F51"]


def test_plan_lets_a_run_age_out_of_the_window() -> None:
    """Same hour-of-week cell, three weeks back is inside the injection's window,
    four weeks back is not (window = restore - 21 days, restore = start - lead)."""
    lead = timedelta(minutes=130)
    inject = utc("2026-11-20T10:00:00Z")
    inside = {"run_id": "in", "t1": sw.fmt(inject - timedelta(days=21) + timedelta(minutes=5)),
              "end": sw.fmt(inject - timedelta(days=21) + timedelta(minutes=10))}
    aged = {"run_id": "out", "t1": sw.fmt(inject - timedelta(days=28) + timedelta(minutes=5)),
            "end": sw.fmt(inject - timedelta(days=28) + timedelta(minutes=10))}
    row = sw.plan_rows(inject, hours=1, lead=lead, days=21, windows=[inside, aged], outages=[])[0]
    assert row["clean"] is False and row["why"] == ["in"]
    row = sw.plan_rows(inject, hours=1, lead=lead, days=21, windows=[aged], outages=[])[0]
    assert row["clean"] is True and row["why"] == []


def test_plan_rounds_a_mid_hour_start_up_to_the_next_hour() -> None:
    rows = sw.plan_rows(utc("2026-11-13T00:20:00Z"), hours=2, lead=sw.DEFAULT_LEAD, days=21,
                        windows=[], outages=[])
    assert [r["inject_at"] for r in rows] == ["2026-11-13T01:00:00Z", "2026-11-13T02:00:00Z"]
    assert all(r["clean"] and r["next_clean"] for r in rows)


# --------------------------------------------------------------------- CLI


def test_cli_runs_from_stdin_with_only_the_standard_library(tmp_path: Path) -> None:
    """The command session pipes the module to the runner host: python3 - plan ..."""
    runs = tmp_path / "runs"
    write(runs / "F51-H-run-1", "result.json", {"t1": "2026-11-11T07:14:52Z", "t2": "2026-11-11T07:32:40Z"})
    ledger = json.dumps({"outages": [
        {"id": "119-stop", "start": "2026-10-30T16:33:00Z", "end": "2026-11-09T08:12:00Z", "source": "s"}]})
    out = subprocess.run(
        [sys.executable, "-I", "-", "plan", "--runs-root", str(runs), "--outages-json", ledger,
         "--from", "2026-11-18T05:30:00Z", "--hours", "3"],
        input=MODULE.read_text(encoding="utf-8"), capture_output=True, text=True, check=True, cwd=tmp_path,
    ).stdout
    lines = out.splitlines()
    assert "실행 1개" in lines[1] and "정지 기록 ok 1건" in lines[1]
    assert "11-18 15:00 | 수 15시 | 깨끗 | 오염 | 다음 칸 F51-H-run-1" in out
    assert "11-18 16:00 | 수 16시 | 오염 | 깨끗 | F51-H-run-1" in out
    assert lines[-1] == "# 이 칸과 다음 칸이 모두 깨끗한 시작 시각 1개: 11-18 17:00"

    window = json.loads(subprocess.run(
        [sys.executable, "-I", str(MODULE), "window", "--runs-root", str(runs), "--outages-json", ledger,
         "--at", "2026-11-13T04:10:00Z"],
        capture_output=True, text=True, check=True, cwd=tmp_path,
    ).stdout)
    assert window["sources"]["outages_path"] == "inline"
    assert [c["runs"] for c in window["injection_cells"]] == [["F51-H-run-1"]]
    assert {c["cell"] for c in window["outage_cells"]} == {5 * 24 + 16, 5 * 24 + 17, 24 + 8, 24 + 9}
