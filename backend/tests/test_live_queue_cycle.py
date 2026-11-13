"""v3 continuous-cycle state machine tests (CYCLE_MODE).

These exercise the cycle-only path added on top of the v2 live queue; the v2
suite in test_live_queue.py proves the default (CYCLE_MODE off) behaviour is
untouched.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from app.live_queue import LiveScenarioQueue
from app.models import RunInfo


NOW = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
CYCLE_IDS = ("F01-R", "F01-H")


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        return self.value


class Coordinator:
    def __init__(self) -> None:
        self.active_lease = None
        self.dirty_run = None

    def snapshot(self):
        return SimpleNamespace(active_lease=self.active_lease, dirty_run=self.dirty_run)


class Scheduler:
    def __init__(self) -> None:
        self.jobs: dict[str, object] = {}
        self.invoker = SimpleNamespace(capture_script=Path("/unused"))

    def snapshot(self):
        return SimpleNamespace(jobs=self.jobs)


class Runner:
    def __init__(self, root: Path, coordinator: Coordinator, scheduler: Scheduler) -> None:
        self.coordinator = coordinator
        self.capture_scheduler = scheduler
        self.artifact_store = SimpleNamespace(root=root)
        self.dispatcher_path = Path("/unused/dispatcher")
        self.catalog_path = Path("/unused/catalog")
        self.scenario_metadata_path = root.parent / "scenario-metadata.json"
        self.is_busy = False
        self.current = None
        self.started: list[tuple[str, str]] = []

    async def start(self, *, scenario_id: str, mode: str, skip_isolation_checks: bool = False):
        self.started.append((scenario_id, mode))
        self.skip_isolation_checks_seen = skip_isolation_checks
        self.current = RunInfo(
            run_id=f"run-{scenario_id}-{len(self.started)}",
            scenario_id=scenario_id,
            mode="run",
            status="running",
            started_at=NOW,
        )
        return self.current

    def get_current(self):
        return self.current

    def ensure_capture_worker(self) -> None:
        pass


def make_cycle_queue(tmp_path: Path, *, cycle_ids=CYCLE_IDS, health_probe=None, **extra):
    clock = Clock()
    coordinator = Coordinator()
    scheduler = Scheduler()
    runner = Runner(tmp_path / "runs", coordinator, scheduler)
    proof = tmp_path / "proof"
    proof.write_text("ready", encoding="utf-8")
    metadata = {
        "title": "Scenario title",
        "description": "AI-authored scenario description.",
        "cause": "Scenario cause",
        "injection_summary": "Scenario injection summary.",
        "user_impact": "Scenario user impact.",
        "distinguishing_evidence": "Scenario distinguishing evidence.",
        # U7: decoys are drawn only for a scenario with a domain (review2 M3).
        "domain": "commerce",
    }
    runner.scenario_metadata_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "scenarios": {sid: metadata for sid in cycle_ids},
            }
        ),
        encoding="utf-8",
    )
    queue = LiveScenarioQueue(
        runner,  # type: ignore[arg-type]
        tmp_path / "live-queue.json",
        clock=clock,
        required_paths={"proof": proof},
        required_env=(),
        cycle_mode=True,
        cycle_scenario_ids=tuple(cycle_ids),
        cycle_health_probe=health_probe,
        **extra,
    )
    queue.ensure_worker = lambda: None  # type: ignore[method-assign]
    queue._controller_evidence_error = lambda _: None  # type: ignore[method-assign]
    return queue, runner, coordinator, scheduler, clock


def test_cycle_covers_the_same_approved_catalogue_as_the_v2_queue(tmp_path: Path) -> None:
    """One approved list, both queues.

    The cycle order used to be a hardcoded tuple of the first seven v3 subjects.
    The registry grew to 44 and nothing reconciled the two, so a v3 capture of
    "the catalogue" would have run seven, reported success, and left 37 out with
    no signal that anything was missing. Promotion must reach both queues.
    """
    queue, _, _, _, _ = make_cycle_queue(tmp_path, cycle_ids=())

    assert queue._cycle_contract()[0] == queue._queue_contract()[0]


def test_cycle_subset_must_come_from_the_approved_list(tmp_path: Path) -> None:
    """A typo in CYCLE_SCENARIOS must refuse, not capture something unasked for."""
    queue, _, _, _, _ = make_cycle_queue(tmp_path, cycle_ids=("F01-R", "F01-TYPO"))

    with pytest.raises(RuntimeError, match="not in the approved list"):
        queue._cycle_contract()

    approved = queue._queue_contract()[0]
    (tmp_path / "subset").mkdir()
    narrowed, _ = make_cycle_queue(tmp_path / "subset", cycle_ids=(approved[1],))[
        0
    ]._cycle_contract()
    assert narrowed == [approved[1]]


@pytest.mark.asyncio
async def test_start_narrows_one_cycle_queue_to_the_requested_scenarios(tmp_path: Path) -> None:
    """The operation harness starts one candidate at a time without restarting the
    container (CYCLE_SCENARIOS is fixed at boot). The request narrows only that
    queue and still refuses ids outside the approved list."""
    approved = make_cycle_queue(tmp_path, cycle_ids=())[0]._queue_contract()[0]
    (tmp_path / "q").mkdir()
    configured = (approved[0], approved[1])
    queue, _, _, _, _ = make_cycle_queue(tmp_path / "q", cycle_ids=configured)

    with pytest.raises(RuntimeError, match="not in the approved list"):
        await queue.start(scenario_ids=["F01-TYPO"])
    with pytest.raises(RuntimeError, match="at least one"):
        await queue.start(scenario_ids=[])

    state = await queue.start(scenario_ids=[approved[1]])

    assert state.scenario_ids == [approved[1]]
    assert state.queue_id.startswith("cycle-1-")
    assert queue._cycle_contract()[0] == list(configured)  # the configured default is untouched


async def _advance_to_injection(queue, runner, clock, *, scenario_id, start=True):
    """start -> cycle_reset -> cycle_normal -> cycle_buffer -> running."""
    if start:
        await queue.start()
    state = await queue.tick()  # cycle_reset -> cycle_normal
    assert state.phase == "cycle_normal"
    assert state.current_scenario_id == scenario_id
    clock.value = state_expected(state)  # normal end (=cycle_start + 2h)
    state = await queue.tick()  # -> cycle_buffer
    assert state.phase == "cycle_buffer"
    clock.value = state_expected(state)  # buffer end
    state = await queue.tick()  # -> running (injection)
    assert state.phase == "running"
    assert runner.started[-1] == (scenario_id, "run")
    return state


def state_expected(state):
    return datetime.strptime(state.expected_transition_at, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


def _complete_injection(runner, scheduler, clock, run_id, *, t1, t2):
    runner.current = runner.current.model_copy(
        update={"status": "succeeded", "finished_at": clock.now(), "exit_code": 0}
    )
    scheduler.jobs[run_id] = SimpleNamespace(status="pending", t1=t1, t2=t2, failure=None)


@pytest.mark.asyncio
async def test_cycle_full_flow_reaches_capture_and_starts_next_cycle(tmp_path: Path) -> None:
    queue, runner, _, scheduler, clock = make_cycle_queue(tmp_path)

    state = await _advance_to_injection(queue, runner, clock, scenario_id="F01-R")
    run_id = state.current_run_id
    _complete_injection(runner, scheduler, clock, run_id,
                        t1="2026-07-16T10:10:00Z", t2="2026-07-16T10:15:00Z")

    state = await queue.tick()  # running -> cycle_cooldown
    assert state.phase == "cycle_cooldown"
    assert state.expected_transition_at == "2026-07-16T10:45:00Z"  # t2 + 30m
    # phases.json must exist at cooldown ENTRY (t2), before the capture job fires
    # at t2 + POST_WINDOW (20m) — a cooldown-end write (t2+30m) would land after
    # the invoker built its args when cooldown (30m) > POST_WINDOW. Regression:
    # 2026-07-25 production run F01-H aborted with --topology-bundle but no
    # --phases-json because phases.json was written 10m too late.
    assert (runner.artifact_store.root / run_id / "phases.json").is_file()

    clock.value = state_expected(state)
    state = await queue.tick()  # cooldown -> waiting_capture
    assert state.phase == "waiting_capture"
    assert (runner.artifact_store.root / run_id / "phases.json").is_file()

    scheduler.jobs[run_id].status = "completed"
    capture = runner.artifact_store.root / run_id / "capture-complete.json"
    capture.write_text("{}\n", encoding="utf-8")
    state = await queue.tick()  # capture done -> next cycle_reset
    assert state.phase == "cycle_reset"
    assert state.next_index == 1
    assert state.completed_run_ids == [run_id]
    assert state.first_gate_passed is True

    # second (final) scenario completes the queue (no re-start; queue is active)
    state = await _advance_to_injection(
        queue, runner, clock, scenario_id="F01-H", start=False
    )
    run_id2 = state.current_run_id
    _complete_injection(runner, scheduler, clock, run_id2,
                        t1="2026-07-16T12:20:00Z", t2="2026-07-16T12:25:00Z")
    state = await queue.tick()  # -> cooldown
    clock.value = state_expected(state)
    state = await queue.tick()  # -> waiting_capture
    scheduler.jobs[run_id2].status = "completed"
    (runner.artifact_store.root / run_id2 / "capture-complete.json").write_text(
        "{}\n", encoding="utf-8"
    )
    state = await queue.tick()  # -> completed
    assert state.phase == "completed"
    assert state.next_index == 2


@pytest.mark.asyncio
async def test_phases_json_records_actual_cycle_timeline(tmp_path: Path) -> None:
    queue, runner, _, scheduler, clock = make_cycle_queue(tmp_path)
    state = await _advance_to_injection(queue, runner, clock, scenario_id="F01-R")
    run_id = state.current_run_id
    _complete_injection(runner, scheduler, clock, run_id,
                        t1="2026-07-16T10:10:00Z", t2="2026-07-16T10:15:00Z")
    state = await queue.tick()  # -> cooldown
    clock.value = state_expected(state)
    await queue.tick()  # -> waiting_capture, writes phases.json

    doc = json.loads((runner.artifact_store.root / run_id / "phases.json").read_text())
    assert doc["schema_version"] == "2.0"
    assert doc["timeline"] == "continuous"
    assert doc["capture_start"] == "2026-07-16T08:00:00Z"      # cycle start
    assert doc["capture_end"] == "2026-07-16T10:45:00Z"        # t2 + 30m
    phases = {p["phase"]: p for p in doc["phases"]}
    assert phases["trainer_reset"]["at"] == "2026-07-16T08:00:00Z"
    # G2META (Q19 A, B): the golden stream and 21-day window fields are always
    # present; with golden reset off no observer was rebooted, so all are null.
    assert phases["trainer_reset"] == {
        "phase": "trainer_reset",
        "at": "2026-07-16T08:00:00Z",
        "golden_id": None,
        "golden_sha256": None,
        "golden_stream_generation": None,
        "golden_stream_boot_load_until": None,
        "golden_stream_valid": None,
        "seasonal_window": None,
    }
    assert phases["normal"] == {
        "phase": "normal", "start": "2026-07-16T08:00:00Z", "end": "2026-07-16T10:00:00Z",
    }
    assert phases["buffer"] == {
        "phase": "buffer", "start": "2026-07-16T10:00:00Z", "end": "2026-07-16T10:10:00Z",
    }
    assert phases["injection"] == {
        "phase": "injection", "start": "2026-07-16T10:10:00Z", "end": "2026-07-16T10:15:00Z",
    }
    assert phases["cooldown"] == {
        "phase": "cooldown", "start": "2026-07-16T10:15:00Z", "end": "2026-07-16T10:45:00Z",
    }


@pytest.mark.asyncio
async def test_restart_resumes_normal_remaining_time_after_process_restart(tmp_path: Path) -> None:
    queue, runner, coordinator, scheduler, clock = make_cycle_queue(tmp_path)
    await queue.start()
    state = await queue.tick()  # -> cycle_normal
    assert state.phase == "cycle_normal"
    assert state.expected_transition_at == "2026-07-16T10:00:00Z"

    # A fresh queue instance (runner process restart) reads persisted state.
    restored = LiveScenarioQueue(
        runner,  # type: ignore[arg-type]
        queue.state_path,
        clock=clock,
        required_paths={},
        required_env=(),
        cycle_mode=True,
        cycle_scenario_ids=CYCLE_IDS,
    )
    restored._controller_evidence_error = lambda _: None  # type: ignore[method-assign]
    assert restored.snapshot().phase == "cycle_normal"

    clock.value = datetime(2026, 7, 16, 9, 59, tzinfo=timezone.utc)
    assert (await restored.tick()).phase == "cycle_normal"  # remaining time honoured
    clock.value = datetime(2026, 7, 16, 10, 0, tzinfo=timezone.utc)
    assert (await restored.tick()).phase == "cycle_buffer"


@pytest.mark.asyncio
async def test_normal_health_failure_restarts_cycle(tmp_path: Path) -> None:
    failures = {"n": 0}

    def probe():
        failures["n"] += 1
        return "observer down" if failures["n"] == 1 else None

    queue, runner, _, _, clock = make_cycle_queue(tmp_path, health_probe=probe)
    await queue.start()
    state = await queue.tick()  # -> cycle_normal
    assert state.phase == "cycle_normal"

    state = await queue.tick()  # health probe fails -> restart cycle
    assert state.phase == "cycle_reset"
    assert state.cycle_restart_counts == {"F01-R": 1}
    assert "observer down" in state.reason
    assert state.cycle_started_at is None

    state = await queue.tick()  # reset -> normal (probe now healthy)
    assert state.phase == "cycle_normal"


@pytest.mark.asyncio
async def test_cycle_restart_budget_exhaustion_pauses(tmp_path: Path) -> None:
    queue, runner, _, _, clock = make_cycle_queue(tmp_path, health_probe=lambda: "always sick")
    await queue.start()
    await queue.tick()  # -> cycle_normal
    state = await queue.tick()  # restart 1
    assert state.cycle_restart_counts == {"F01-R": 1}
    await queue.tick()  # reset -> normal
    state = await queue.tick()  # restart 2
    assert state.cycle_restart_counts == {"F01-R": 2}
    await queue.tick()  # reset -> normal
    state = await queue.tick()  # restart 3 == over budget -> pause
    assert state.phase == "paused"
    assert "restart budget exhausted" in state.reason
    assert state.cycle_restart_counts == {"F01-R": 2}


class FakeRestoreRunner:
    def __init__(self, restore_stdout: str = "") -> None:
        self.remotes: list[str] = []
        self.restore_stdout = restore_stdout

    def __call__(self, cmd, **kwargs):
        self.remotes.append(cmd[-1])
        stdout = self.restore_stdout if "--golden-root" in cmd[-1] else ""
        return subprocess.CompletedProcess(cmd, 0, stdout, "")


GOLDEN_SHA = "0123456789abcdef" * 4


@pytest.mark.asyncio
async def test_trainer_reset_thaw_freeze_thaw_ordering_when_golden_enabled(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MODEL_SNAPSHOT_SSH_TARGET", "ydkim@192.168.230.119")
    queue, runner, _, scheduler, clock = make_cycle_queue(tmp_path)
    restore = FakeRestoreRunner()
    ssh_key = queue.state_path.parent / "ssh_key"
    ssh_key.write_text("key", encoding="utf-8")
    queue.golden_reset_enabled = True
    queue.restore_retry_delay_sec = 0
    queue._restore_runner = restore  # type: ignore[assignment]
    queue.required_paths["ssh_key"] = ssh_key

    state = await _advance_to_injection(queue, runner, clock, scenario_id="F01-R")
    run_id = state.current_run_id
    _complete_injection(runner, scheduler, clock, run_id,
                        t1="2026-07-16T10:10:00Z", t2="2026-07-16T10:15:00Z")
    state = await queue.tick()  # -> cooldown
    clock.value = state_expected(state)
    state = await queue.tick()  # -> waiting_capture
    scheduler.jobs[run_id].status = "completed"
    (runner.artifact_store.root / run_id / "capture-complete.json").write_text(
        "{}\n", encoding="utf-8"
    )
    await queue.tick()  # capture done -> thaw + next reset

    kinds = []
    for remote in restore.remotes:
        if "--golden-root" in remote:
            kinds.append("restore")
        elif "--freeze" in remote:
            kinds.append("freeze")
        elif "--thaw" in remote:
            kinds.append("thaw")
    # reset restores golden then thaws for the 2h lead-in; buffer freezes;
    # capture-complete thaws again.
    assert kinds[:4] == ["restore", "thaw", "freeze", "thaw"]


@pytest.mark.asyncio
async def test_phases_json_carries_golden_sha256_from_restore_result(
    tmp_path: Path, monkeypatch
) -> None:
    """R2: the restore result's golden_sha256 reaches phases.json trainer_reset,
    which the capture copies verbatim into the recording meta."""
    monkeypatch.setenv("MODEL_SNAPSHOT_SSH_TARGET", "ydkim@192.168.230.119")
    queue, runner, _, scheduler, clock = make_cycle_queue(tmp_path)
    restore = FakeRestoreRunner(
        json.dumps(
            {
                "action": "restore-golden",
                "status": "ok",
                "golden_dir": "/data/eval-cases/goldens/golden-0900",
                "golden_sha256": GOLDEN_SHA,
            }
        )
    )
    ssh_key = queue.state_path.parent / "ssh_key"
    ssh_key.write_text("key", encoding="utf-8")
    queue.golden_reset_enabled = True
    queue.restore_retry_delay_sec = 0
    queue._restore_runner = restore  # type: ignore[assignment]
    queue.required_paths["ssh_key"] = ssh_key

    state = await _advance_to_injection(queue, runner, clock, scenario_id="F01-R")
    assert state.cycle_golden_id == "golden-0900"
    assert state.cycle_golden_sha256 == GOLDEN_SHA
    run_id = state.current_run_id
    _complete_injection(runner, scheduler, clock, run_id,
                        t1="2026-07-16T10:10:00Z", t2="2026-07-16T10:15:00Z")
    state = await queue.tick()  # -> cooldown, writes phases.json

    doc = json.loads((runner.artifact_store.root / run_id / "phases.json").read_text())
    reset = next(p for p in doc["phases"] if p["phase"] == "trainer_reset")
    seasonal = reset.pop("seasonal_window")
    assert reset == {
        "phase": "trainer_reset",
        "at": "2026-07-16T08:00:00Z",
        "golden_id": "golden-0900",
        "golden_sha256": GOLDEN_SHA,
        # A restore result without the G2META stream fields leaves them unknown.
        "golden_stream_generation": None,
        "golden_stream_boot_load_until": None,
        "golden_stream_valid": None,
    }
    assert seasonal["status"] == "ok" and seasonal["end"] == "2026-07-16T08:00:00Z"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"golden_dir": "/g/golden-0900", "golden_sha256": GOLDEN_SHA},
         {"id": "golden-0900", "sha256": GOLDEN_SHA}),
        # restore script that predates the digest
        ({"golden_dir": "/g/golden-0900"}, {"id": "golden-0900", "sha256": None}),
        # not a sha256 hex digest
        ({"golden_dir": "/g/golden-0900", "golden_sha256": "abc"},
         {"id": "golden-0900", "sha256": None}),
        ({"golden_dir": "/g/golden-0900", "golden_sha256": GOLDEN_SHA.upper()},
         {"id": "golden-0900", "sha256": None}),
        ({"golden_dir": "/g/golden-0900", "golden_sha256": 123},
         {"id": "golden-0900", "sha256": None}),
    ],
)
def test_parse_golden_result_reads_golden_sha256(payload, expected) -> None:
    result = subprocess.CompletedProcess(["ssh"], 0, json.dumps(payload), "")
    assert LiveScenarioQueue._parse_golden_result(result) == expected


def test_parse_golden_result_tolerates_non_object_stdout() -> None:
    result = subprocess.CompletedProcess(["ssh"], 0, "[]", "")
    assert LiveScenarioQueue._parse_golden_result(result) is None


# ---- G2META (Q19 A, B): golden stream validity and the 21-day seasonal window ----


def _g2meta_queue(tmp_path: Path, monkeypatch, restore_payload: dict):
    monkeypatch.setenv("MODEL_SNAPSHOT_SSH_TARGET", "ydkim@192.168.230.119")
    queue, runner, _, scheduler, clock = make_cycle_queue(tmp_path)
    restore = FakeRestoreRunner(json.dumps({
        "action": "restore-golden", "status": "ok",
        "golden_dir": "/data/eval-cases/goldens/2026-11-12T164709Z", "golden_sha256": GOLDEN_SHA,
        **restore_payload,
    }))
    ssh_key = queue.state_path.parent / "ssh_key"
    ssh_key.write_text("key", encoding="utf-8")
    queue.golden_reset_enabled = True
    queue.restore_retry_delay_sec = 0
    queue._restore_runner = restore  # type: ignore[assignment]
    queue.required_paths["ssh_key"] = ssh_key
    ledger = tmp_path / "observation-outages.json"
    ledger.write_text(json.dumps({"outages": [{
        "id": "observer-stop", "start": "2026-07-12T03:30:00Z", "end": "2026-07-13T05:20:00Z",
        "source": "test ledger"}]}), encoding="utf-8")
    queue.observation_outages_path = ledger
    root = runner.artifact_store.root
    # A week earlier, same Thursday 10 UTC cell as this cycle's injection.
    for name, t1, t2 in (
        ("F72-R-run-aaaa0001", "2026-07-09T10:05:00Z", "2026-07-09T10:40:00Z"),
        # Older than 21 days before the restore: outside the window.
        ("F63-R-run-aaaa0002", "2026-06-20T10:05:00Z", "2026-06-20T10:30:00Z"),
    ):
        (root / name).mkdir(parents=True)
        (root / name / "result.json").write_text(json.dumps({"t1": t1, "t2": t2}), encoding="utf-8")
    return queue, runner, scheduler, clock


async def _g2meta_phases(queue, runner, scheduler, clock, *, t1, t2):
    state = await _advance_to_injection(queue, runner, clock, scenario_id="F01-R")
    run_id = state.current_run_id
    _complete_injection(runner, scheduler, clock, run_id, t1=t1, t2=t2)
    await queue.tick()  # -> cooldown, writes phases.json
    doc = json.loads((runner.artifact_store.root / run_id / "phases.json").read_text())
    return state, next(p for p in doc["phases"] if p["phase"] == "trainer_reset")


@pytest.mark.asyncio
async def test_phases_json_records_golden_stream_validity_and_the_21_day_window(
    tmp_path: Path, monkeypatch
) -> None:
    queue, runner, scheduler, clock = _g2meta_queue(tmp_path, monkeypatch, {
        "restored_at": "2026-07-16T08:01:00Z",
        "golden_stream_generation": 1985,
        "golden_stream_boot_load_until": "2026-07-20T16:24:00Z",
    })
    state, reset = await _g2meta_phases(queue, runner, scheduler, clock,
                                        t1="2026-07-16T10:10:00Z", t2="2026-07-16T10:15:00Z")
    assert state.cycle_golden_stream == {
        "generation": 1985, "boot_load_until": "2026-07-20T16:24:00Z",
        "restored_at": "2026-07-16T08:01:00Z", "valid": True,
    }
    assert reset["golden_id"] == "2026-11-12T164709Z"
    assert reset["golden_stream_generation"] == 1985
    assert reset["golden_stream_boot_load_until"] == "2026-07-20T16:24:00Z"
    assert reset["golden_stream_valid"] is True
    window = reset["seasonal_window"]
    # The window ends at the restore the result reports, not the queue clock.
    assert (window["status"], window["days"], window["start"], window["end"]) == (
        "ok", 21, "2026-06-25T08:01:00Z", "2026-07-16T08:01:00Z")
    thu, sun, mon = 4 * 24, 0, 24
    assert window["injection_cells"] == [{"cell": thu + 10, "kst": "목 19시", "runs": ["F72-R-run-aaaa0001"]}]
    assert [c["cell"] for c in window["outage_cells"]] == [sun + 3, sun + 4, mon + 5, mon + 6]
    assert [r["run_id"] for r in window["runs_counted"]] == ["F72-R-run-aaaa0001"]
    assert window["sources"]["outages_status"] == "ok"
    assert window["this_injection_cells"] == [{"cell": thu + 10, "kst": "목 19시"}]
    assert window["overlap"] is True
    assert window["overlap_cells"] == [{"cell": thu + 10, "kst": "목 19시"}]


@pytest.mark.asyncio
async def test_expired_golden_stream_and_a_clean_injection_cell(tmp_path: Path, monkeypatch) -> None:
    queue, runner, scheduler, clock = _g2meta_queue(tmp_path, monkeypatch, {
        "restored_at": "2026-07-16T08:01:00Z",
        "golden_stream_generation": 1985,
        # Seven days after the capture: the observer refuses the golden generation.
        "golden_stream_boot_load_until": "2026-07-16T08:00:59Z",
    })
    _, reset = await _g2meta_phases(queue, runner, scheduler, clock,
                                    t1="2026-07-16T11:10:00Z", t2="2026-07-16T11:15:00Z")
    assert reset["golden_stream_valid"] is False
    assert reset["seasonal_window"]["overlap"] is False
    assert reset["seasonal_window"]["overlap_cells"] == []


@pytest.mark.asyncio
async def test_restore_without_stream_fields_falls_back_to_the_queue_clock(
    tmp_path: Path, monkeypatch
) -> None:
    queue, runner, scheduler, clock = _g2meta_queue(tmp_path, monkeypatch, {})
    queue.observation_outages_path = tmp_path / "absent.json"
    state, reset = await _g2meta_phases(queue, runner, scheduler, clock,
                                        t1="2026-07-16T10:10:00Z", t2="2026-07-16T10:15:00Z")
    assert state.cycle_golden_stream == {
        "generation": None, "boot_load_until": None, "restored_at": "2026-07-16T08:00:00Z", "valid": None,
    }
    assert reset["golden_stream_valid"] is None
    window = reset["seasonal_window"]
    assert window["end"] == "2026-07-16T08:00:00Z"
    assert window["sources"]["outages_status"] == "missing"
    assert window["outage_cells"] == []
    assert any("관측 정지 기록을 읽지 못했다" in line for line in window["limits"])


@pytest.mark.asyncio
async def test_a_failing_window_computation_never_blocks_the_cycle(tmp_path: Path, monkeypatch) -> None:
    from app import seasonal_window

    def boom(*_args, **_kwargs):
        raise OSError("runs root unreadable")

    monkeypatch.setattr(seasonal_window, "compute", boom)
    queue, runner, scheduler, clock = _g2meta_queue(tmp_path, monkeypatch, {
        "restored_at": "2026-07-16T08:01:00Z",
        "golden_stream_generation": 1985,
        "golden_stream_boot_load_until": "2026-07-20T16:24:00Z",
    })
    _, reset = await _g2meta_phases(queue, runner, scheduler, clock,
                                    t1="2026-07-16T10:10:00Z", t2="2026-07-16T10:15:00Z")
    assert reset["seasonal_window"] == {"status": "error", "error": "OSError: runs root unreadable"}
    assert reset["golden_stream_valid"] is True


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"golden_stream_generation": 1985, "golden_stream_boot_load_until": "2026-11-19T16:24:00Z",
          "restored_at": "2026-11-12T19:00:00Z"},
         {"generation": 1985, "boot_load_until": "2026-11-19T16:24:00Z", "restored_at": "2026-11-12T19:00:00Z"}),
        # KST offsets are normalized to UTC.
        ({"golden_stream_generation": 0, "golden_stream_boot_load_until": "2026-11-20T01:24:00+09:00"},
         {"generation": 0, "boot_load_until": "2026-11-19T16:24:00Z", "restored_at": None}),
        ({}, {"generation": None, "boot_load_until": None, "restored_at": None}),
        ({"golden_stream_generation": True, "golden_stream_boot_load_until": "2026-11-19 16:24"},
         {"generation": None, "boot_load_until": None, "restored_at": None}),
        ({"golden_stream_generation": -1, "golden_stream_boot_load_until": "2026-11-19T16:24:00"},
         {"generation": None, "boot_load_until": None, "restored_at": None}),
        ({"golden_stream_generation": "1985", "restored_at": 5},
         {"generation": None, "boot_load_until": None, "restored_at": None}),
    ],
)
def test_parse_golden_stream_reads_the_restore_result(payload, expected) -> None:
    result = subprocess.CompletedProcess(["ssh"], 0, json.dumps(payload), "")
    assert LiveScenarioQueue._parse_golden_stream(result) == expected


def test_parse_golden_stream_tolerates_non_object_stdout() -> None:
    empty = {"generation": None, "boot_load_until": None, "restored_at": None}
    assert LiveScenarioQueue._parse_golden_stream(subprocess.CompletedProcess(["ssh"], 0, "[]", "")) == empty
    assert LiveScenarioQueue._parse_golden_stream(subprocess.CompletedProcess(["ssh"], 0, "not json", "")) == empty
    assert LiveScenarioQueue._parse_golden_stream(None) == empty


@pytest.mark.asyncio
async def test_resume_from_pause_reenters_cycle_reset(tmp_path: Path) -> None:
    queue, runner, _, _, clock = make_cycle_queue(tmp_path)
    await queue.start()
    state = await queue.tick()  # -> cycle_normal
    paused = queue._pause(state, "operator halt")
    assert paused.phase == "paused"

    resumed = await queue.resume()
    assert resumed.phase == "cycle_reset"
    assert resumed.current_scenario_id is None
    assert resumed.cycle_started_at is None


@pytest.mark.asyncio
async def test_cycle_restart_excuses_failed_run_from_clean_window(tmp_path: Path) -> None:
    queue, runner, _, scheduler, clock = make_cycle_queue(tmp_path)
    state = await _advance_to_injection(queue, runner, clock, scenario_id="F01-R")
    run_id = state.current_run_id

    # Injection produced a failed (non-dirty) run -> cycle restarts.
    runner.current = runner.current.model_copy(update={"status": "failed", "dirty": False})
    state = await queue.tick()
    assert state.phase == "cycle_reset"
    assert state.cycle_restart_counts == {"F01-R": 1}

    marker = runner.artifact_store.root / run_id / "clean-window-excused.json"
    assert marker.is_file()
    doc = json.loads(marker.read_text())
    assert doc["reason"] == "cycle-restart"
    assert doc["scenario_id"] == "F01-R"
    assert doc["excused_at"].endswith("Z")


@pytest.mark.asyncio
async def test_cycle_capture_complete_excuses_run_from_clean_window(tmp_path: Path) -> None:
    queue, runner, _, scheduler, clock = make_cycle_queue(tmp_path)
    state = await _advance_to_injection(queue, runner, clock, scenario_id="F01-R")
    run_id = state.current_run_id
    _complete_injection(runner, scheduler, clock, run_id,
                        t1="2026-07-16T10:10:00Z", t2="2026-07-16T10:15:00Z")
    state = await queue.tick()  # -> cooldown
    clock.value = state_expected(state)
    await queue.tick()  # -> waiting_capture

    scheduler.jobs[run_id].status = "completed"
    (runner.artifact_store.root / run_id / "capture-complete.json").write_text(
        "{}\n", encoding="utf-8"
    )
    state = await queue.tick()  # capture done -> next cycle_reset
    assert state.phase == "cycle_reset"

    marker = runner.artifact_store.root / run_id / "clean-window-excused.json"
    assert marker.is_file()
    assert json.loads(marker.read_text())["reason"] == "cycle-complete"


@pytest.mark.asyncio
async def test_v2_path_never_writes_cycle_clean_window_excuse(tmp_path: Path) -> None:
    # The v2 queue (CYCLE_MODE off) must not gain the cycle exemption marker on
    # its normal capture-complete path.
    from tests.test_live_queue import make_queue

    queue, runner, _, scheduler, clock = make_queue(tmp_path)
    await queue.start()
    state = await queue.tick()
    run_id = state.current_run_id
    runner.current = runner.current.model_copy(
        update={"status": "succeeded", "finished_at": clock.now(), "exit_code": 0}
    )
    scheduler.jobs[run_id] = SimpleNamespace(
        status="pending", t2="2026-07-16T08:10:00Z", failure=None
    )
    await queue.tick()  # -> waiting_capture
    scheduler.jobs[run_id].status = "completed"
    capture = runner.artifact_store.root / run_id / "capture-complete.json"
    capture.parent.mkdir(parents=True, exist_ok=True)
    capture.write_text("{}\n", encoding="utf-8")
    runner.current = None
    state = await queue.tick()  # -> waiting_clean_window (v2)

    assert state.phase == "waiting_clean_window"
    assert not (runner.artifact_store.root / run_id / "clean-window-excused.json").is_file()


class FakeTopologyCollector:
    def __init__(self, bundle_dir: Path) -> None:
        self.bundle_dir = Path(bundle_dir)
        self.started = False
        self.stopped = False
        self.run_id = None

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def set_run_id(self, run_id: str) -> None:
        self.run_id = run_id


@pytest.mark.asyncio
async def test_cycle_injection_writes_topology_bundle_marker(tmp_path: Path) -> None:
    created: list[FakeTopologyCollector] = []

    def factory(bundle_dir):
        collector = FakeTopologyCollector(bundle_dir)
        created.append(collector)
        return collector

    queue, runner, _, scheduler, clock = make_cycle_queue(tmp_path)
    queue.topology_collector_factory = factory

    state = await _advance_to_injection(queue, runner, clock, scenario_id="F01-R")
    run_id = state.current_run_id

    # Collector started at cycle_reset, bound + marker written at injection.
    assert created and created[0].started is True
    assert "cycle-topology" in str(created[0].bundle_dir)
    assert created[0].run_id == run_id
    marker = runner.artifact_store.root / run_id / "topology-bundle.path"
    assert marker.is_file()
    assert marker.read_text().strip() == str(created[0].bundle_dir)

    # Collector is stopped when the cycle's capture completes.
    _complete_injection(runner, scheduler, clock, run_id,
                        t1="2026-07-16T10:10:00Z", t2="2026-07-16T10:15:00Z")
    state = await queue.tick()  # -> cooldown
    clock.value = state_expected(state)
    await queue.tick()  # -> waiting_capture
    scheduler.jobs[run_id].status = "completed"
    (runner.artifact_store.root / run_id / "capture-complete.json").write_text(
        "{}\n", encoding="utf-8"
    )
    await queue.tick()  # capture done
    assert created[0].stopped is True


@pytest.mark.asyncio
async def test_v2_path_never_writes_topology_bundle_marker(tmp_path: Path) -> None:
    from tests.test_live_queue import make_queue

    queue, runner, _, scheduler, clock = make_queue(tmp_path)
    # Even if a factory were present, the v2 path must never touch it.
    queue.topology_collector_factory = lambda bundle_dir: FakeTopologyCollector(bundle_dir)
    await queue.start()
    state = await queue.tick()
    run_id = state.current_run_id

    assert not (runner.artifact_store.root / run_id / "topology-bundle.path").is_file()
    assert queue._topology_collector is None


@pytest.mark.asyncio
async def test_cycle_injection_opts_out_of_isolation_checks(tmp_path: Path) -> None:
    queue, runner, _, _, clock = make_cycle_queue(tmp_path)
    await _advance_to_injection(queue, runner, clock, scenario_id="F01-R")
    # The cycle path tells the runner to skip clean-window / scenario_overlap so a
    # prior cycle run left inside the 30m overlap window cannot block injection.
    assert runner.skip_isolation_checks_seen is True


@pytest.mark.asyncio
async def test_resume_resets_cycle_restart_counts(tmp_path: Path) -> None:
    queue, runner, _, _, clock = make_cycle_queue(tmp_path)
    await queue.start()
    state = await queue.tick()  # -> cycle_normal
    # Simulate a scenario that already burned its restart budget.
    queue._write(state.model_copy(update={"cycle_restart_counts": {"F01-R": 2}}))
    paused = queue._pause(queue.snapshot(), "operator halt")
    assert paused.phase == "paused"

    resumed = await queue.resume()
    assert resumed.phase == "cycle_reset"
    assert resumed.cycle_restart_counts == {}


@pytest.mark.asyncio
async def test_cancel_closes_paused_queue_so_next_start_works(tmp_path: Path) -> None:
    queue, runner, _, _, clock = make_cycle_queue(tmp_path)
    await queue.start()
    state = await queue.tick()  # -> cycle_normal
    queue._pause(state, "DIRTY coordinator state blocks the live queue")

    with pytest.raises(RuntimeError, match="paused"):
        await queue.start()

    cancelled = await queue.cancel(reason="scenario on hold")
    assert cancelled.phase == "completed"
    assert cancelled.current_scenario_id is None
    assert cancelled.current_run_id is None
    assert "scenario on hold" in cancelled.reason
    assert "DIRTY coordinator state" in cancelled.reason

    restarted = await queue.start()
    assert restarted.phase == "cycle_reset"
    assert restarted.queue_id != cancelled.queue_id


@pytest.mark.asyncio
async def test_cancel_refuses_unpaused_queue(tmp_path: Path) -> None:
    queue, runner, _, _, clock = make_cycle_queue(tmp_path)
    await queue.start()
    await queue.tick()  # -> cycle_normal
    with pytest.raises(RuntimeError, match="only a paused"):
        await queue.cancel()
    assert queue.snapshot().phase == "cycle_normal"


@pytest.mark.asyncio
async def test_collector_restart_wipes_stale_bundle_dir(tmp_path: Path) -> None:
    gen = {"n": 0}

    def factory(bundle_dir):
        directory = Path(bundle_dir)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"gen{gen['n']}.json").write_text("x", encoding="utf-8")
        gen["n"] += 1
        return FakeTopologyCollector(bundle_dir)

    queue, runner, _, _, clock = make_cycle_queue(tmp_path)
    queue.topology_collector_factory = factory
    await queue.start()
    await queue.tick()  # cycle_reset -> normal, starts collector (writes gen0)

    bundle = queue._cycle_topology_dir(queue.snapshot())
    assert (bundle / "gen0.json").is_file()
    # An orphan left by the previous collector instance (manifest can't cover it).
    (bundle / "orphan.json").write_text("stale", encoding="utf-8")

    # A restart reuses the same bundle dir; the wipe must clear stale files so the
    # fresh manifest exactly describes the directory (capture ingest contract).
    queue._start_cycle_topology(queue.snapshot())
    assert not (bundle / "gen0.json").exists()
    assert not (bundle / "orphan.json").exists()
    assert (bundle / "gen1.json").is_file()


@pytest.mark.asyncio
async def test_start_route_passes_the_optional_subset_body(monkeypatch) -> None:
    """The web button posts no body; the operation harness posts {"scenario_ids": [...]}."""
    from httpx import ASGITransport, AsyncClient

    from app import main

    calls = []

    class FakeQueue:
        async def start(self, scenario_ids=None):
            calls.append(scenario_ids)
            raise RuntimeError("stop here")

    monkeypatch.setattr(main, "get_live_queue", lambda: FakeQueue())
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://t") as client:
        no_body = await client.post("/api/live-queue/start")
        subset = await client.post("/api/live-queue/start", json={"scenario_ids": ["F30-R"]})

    assert (no_body.status_code, subset.status_code) == (409, 409)
    assert calls == [None, ["F30-R"]]


# ---------------------------------------------------------------------------
# U7: decoy and routine deployments inside the cycle
# ---------------------------------------------------------------------------
from app.decoys import build_schedule, cycle_seed, load_decoy_policy  # noqa: E402
from tests.test_decoys import policy_document  # noqa: E402

CLEAN_SIGNALS = {
    "baseline_loadgen_alive": 1.0,
    "achieved_rps": 4.0,
    "diurnal_rps_floor": 1.0,
    "user_5xx_rate": 0.0,
    "entry_p95_sec": 0.05,
    "prev_pool_pending": 0.0,
}


class DecoyAwareProbe:
    """Preflight probe double: the alerts and open incidents it sees, and the
    same alarm-filter hook the production probe exposes."""

    def __init__(self, alerts=(), open_incidents=0) -> None:
        self.alerts = list(alerts)
        self.open_incidents = open_incidents
        self.alarm_filter = None
        self.last_excused_alarms: list[dict] = []
        self.filters_seen: list[bool] = []

    def set_alarm_filter(self, alarm_filter) -> None:
        self.alarm_filter = alarm_filter

    def collect(self, *, now):
        self.filters_seen.append(self.alarm_filter is not None)
        remaining, self.last_excused_alarms = (
            self.alarm_filter(self.alerts) if self.alarm_filter else (self.alerts, [])
        )
        return {
            **CLEAN_SIGNALS,
            "active_alarms": float(len(remaining)),
            "open_incidents": float(self.open_incidents),
        }


class FakeDecoyExecutor:
    def __init__(self, *, polls_until_done=1, never_done=()) -> None:
        self.applied: list[str] = []
        self.undone: list[str] = []
        self.polls: dict[str, int] = {}
        self.polls_until_done = polls_until_done
        self.never_done = set(never_done)

    def apply(self, record) -> None:
        self.applied.append(record.id)

    def rollout_done(self, record) -> bool:
        self.polls[record.id] = self.polls.get(record.id, 0) + 1
        return record.id not in self.never_done and self.polls[record.id] >= self.polls_until_done

    def undo(self, record) -> None:
        self.undone.append(record.id)


def _decoy_queue(
    tmp_path: Path, monkeypatch=None, *, seed=None, probe=None, executor=None, **policy_overrides
):
    policy_path = tmp_path / "decoy-deployments.json"
    policy_path.write_text(json.dumps(policy_document(**policy_overrides)), encoding="utf-8")
    if seed is not None:
        monkeypatch.setattr("app.live_queue.cycle_seed", lambda *args: seed)
    executor = executor or FakeDecoyExecutor()
    queue, runner, coordinator, scheduler, clock = make_cycle_queue(
        tmp_path,
        decoy_policy_path=policy_path,
        decoy_executor=executor,
        preflight_probe=probe,
    )
    return queue, runner, scheduler, clock, executor, policy_path


def _seed_with(predicate) -> int:
    policy, _ = load_decoy_policy_from_document()
    for seed in range(5000):
        records = build_schedule(
            policy, seed=seed, scenario_id="F01-R", scenario={"domain": "commerce"},
            cycle_start=NOW, buffer_start=NOW + timedelta(hours=2),
        )
        if predicate(records):
            return seed
    raise AssertionError("no seed satisfies the predicate")


def load_decoy_policy_from_document():
    from app.decoys import DecoyPolicy

    return DecoyPolicy.model_validate(policy_document()), None


def _parse(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


async def _walk_normal(queue, clock, *, step=timedelta(seconds=30)):
    """Tick through the 2h normal phase the way the worker loop would."""
    state = queue.snapshot()
    end = _parse(state.expected_transition_at)
    while clock.value < end:
        clock.value = min(clock.value + step, end)
        state = await queue.tick()
        if state.phase != "cycle_normal":
            break
    return state


@pytest.mark.asyncio
async def test_cycle_plans_decoys_from_the_recorded_seed(tmp_path: Path) -> None:
    """Completion condition 1: the schedule is the seeded one, reproducible from
    values the recording keeps (queue id, scenario, cycle start)."""
    queue, _, _, _, _, policy_path = _decoy_queue(tmp_path)
    await queue.start()
    state = await queue.tick()  # -> cycle_normal

    seed = cycle_seed("testbed-v4-decoy", state.queue_id, "F01-R", state.cycle_started_at)
    policy, digest = load_decoy_policy(policy_path)
    registry = json.loads(queue.runner.scenario_metadata_path.read_text())["scenarios"]["F01-R"]
    expected = build_schedule(
        policy, seed=seed, scenario_id="F01-R", scenario=registry,
        cycle_start=NOW, buffer_start=NOW + timedelta(hours=2),
    )
    assert state.cycle_decoy_seed == seed
    assert state.cycle_decoy_policy_sha256 == digest
    assert state.cycle_decoys == expected


@pytest.mark.asyncio
async def test_cycle_runs_decoys_on_time_and_records_them_for_the_capture(
    tmp_path: Path, monkeypatch
) -> None:
    seed = _seed_with(
        lambda records: len(records) >= 2
        and any(r.kind in {"restart", "config_rollout"} for r in records)
        and {r.window for r in records} == {"normal", "near_fault"}
    )
    probe = DecoyAwareProbe()
    queue, runner, _, clock, executor, _ = _decoy_queue(
        tmp_path, monkeypatch, seed=seed, probe=probe
    )
    await queue.start()
    state = await queue.tick()  # -> cycle_normal
    planned = list(state.cycle_decoys)

    state = await _walk_normal(queue, clock)
    assert state.phase == "cycle_buffer"
    assert executor.applied == [record.id for record in planned]
    for before, after in zip(planned, state.cycle_decoys):
        assert after.status == "applied"
        started = _parse(after.started_at)
        assert _parse(before.planned_at) <= started <= _parse(before.planned_at) + timedelta(seconds=30)
        assert _parse(after.finished_at) < _parse(state.cycle_buffer_started_at)

    clock.value = state_expected(state)
    state = await queue.tick()  # buffer end -> running
    assert state.phase == "running"
    document = json.loads(
        (runner.artifact_store.root / state.current_run_id / "decoys.json").read_text()
    )
    assert document["seed"] == seed
    assert document["scenario_id"] == "F01-R"
    assert document["cycle_start"] == "2026-07-16T08:00:00Z"
    assert [item["id"] for item in document["decoys"]] == [r.id for r in planned]
    assert all(item["status"] == "applied" for item in document["decoys"])
    preflight = json.loads(
        (runner.artifact_store.root / state.current_run_id / "preflight.json").read_text()
    )
    assert preflight["verdict"] == "clean"
    assert preflight["excused_alarms"] == []
    assert probe.filters_seen == [True]


@pytest.mark.asyncio
async def test_missed_decoys_are_skipped_and_a_stuck_rollout_is_undone(
    tmp_path: Path, monkeypatch
) -> None:
    seed = _seed_with(
        lambda records: len(records) >= 2
        and records[0].kind in {"restart", "config_rollout"}
    )
    queue, _, _, clock, executor, _ = _decoy_queue(
        tmp_path, monkeypatch, seed=seed,
        executor=FakeDecoyExecutor(never_done={"d1"}),
    )
    await queue.start()
    state = await queue.tick()
    first = state.cycle_decoys[0]

    clock.value = _parse(first.planned_at)
    state = await queue.tick()
    assert state.cycle_decoys[0].status == "rolling"
    clock.value += timedelta(seconds=420)  # rollout timeout
    state = await queue.tick()
    assert state.cycle_decoys[0].status == "rolled_back"
    assert executor.undone == ["d1"]

    # The runner was away until the normal phase ended: nothing runs late into
    # the buffer, and the misses are recorded rather than dropped.
    clock.value = state_expected(state)
    state = await queue.tick()
    assert state.phase == "cycle_buffer"
    assert all(record.status == "skipped" for record in state.cycle_decoys[1:])
    assert executor.applied == ["d1"]


@pytest.mark.asyncio
async def test_a_policy_domain_without_domain_words_pauses_with_the_reason(tmp_path: Path) -> None:
    """U7b review M2: a decoy policy with a domain the runner has no domain
    words for is refused, not read as "never named". The queue pauses before
    any decoy is planned and says why."""
    queue, _, _, _, executor, _ = _decoy_queue(
        tmp_path,
        domain_namespaces={"commerce": "rca-testbed-commerce", "logistics": "rca-testbed-logistics"},
    )
    await queue.start()
    state = await queue.tick()
    assert state.phase == "paused"
    assert "decoy plan failed for F01-R" in state.reason
    assert "logistics" in state.reason and "no domain words" in state.reason
    assert state.cycle_decoys == [] and executor.applied == []


@pytest.mark.asyncio
async def test_decoys_off_keeps_the_former_records(tmp_path: Path) -> None:
    """Without a policy the runner writes no decoys.json and the preflight
    record keeps exactly its former keys (meta.json additions only)."""
    probe = DecoyAwareProbe()
    queue, runner, _, _, clock = make_cycle_queue(tmp_path, preflight_probe=probe)
    state = await _advance_to_injection(queue, runner, clock, scenario_id="F01-R")

    run_dir = runner.artifact_store.root / state.current_run_id
    assert state.cycle_decoys == [] and state.cycle_decoy_seed is None
    assert not (run_dir / "decoys.json").exists()
    preflight = json.loads((run_dir / "preflight.json").read_text())
    assert set(preflight) == {"window", "verdict", "checked_at", "waited_sec", "checks", "ai_judgement"}
    assert probe.filters_seen == [False]


def _decoy_alarm(record, *, service=None):
    return {
        "state": "firing",
        "labels": {"alertname": "latency", "service_name": service or record.service_name},
        "activeAt": record.started_at,
    }


# The excuse holds only while the preflight runs within the grace of the decoy's
# finish. A near-fault decoy finishes at least ~22 minutes before the preflight,
# so the excuse path is exercised with a policy grace of 40 minutes; the shipped
# 15 minutes is checked by the test that a lingering decoy alarm blocks.
WIDE_GRACE = 2400


async def _to_buffer_end_with_applied_decoy(tmp_path, monkeypatch, probe, *, grace=WIDE_GRACE):
    seed = _seed_with(
        lambda records: any(
            r.kind in {"restart", "config_rollout"} and r.window == "near_fault" for r in records
        )
    )
    queue, runner, scheduler, clock, executor, _ = _decoy_queue(
        tmp_path, monkeypatch, seed=seed, probe=probe, alarm_excuse_grace_sec=grace
    )
    await queue.start()
    await queue.tick()
    state = await _walk_normal(queue, clock)
    assert state.phase == "cycle_buffer"
    decoy = [
        r for r in state.cycle_decoys if r.kind in {"restart", "config_rollout"} and r.window == "near_fault"
    ][-1]
    clock.value = state_expected(state)
    return queue, runner, state, decoy, clock


@pytest.mark.asyncio
async def test_preflight_excuses_the_alarm_a_decoy_rollout_explains(tmp_path: Path, monkeypatch) -> None:
    probe = DecoyAwareProbe()
    queue, runner, _, decoy, _ = await _to_buffer_end_with_applied_decoy(tmp_path, monkeypatch, probe)
    probe.alerts = [_decoy_alarm(decoy)]

    state = await queue.tick()

    assert state.phase == "running"
    preflight = json.loads(
        (runner.artifact_store.root / state.current_run_id / "preflight.json").read_text()
    )
    assert preflight["verdict"] == "clean"
    assert {c["name"]: c["value"] for c in preflight["checks"]}["active_alarms"] == 0.0
    assert [item["decoy_id"] for item in preflight["excused_alarms"]] == [decoy.id]


@pytest.mark.asyncio
async def test_preflight_still_refuses_an_alarm_no_decoy_explains(tmp_path: Path, monkeypatch) -> None:
    probe = DecoyAwareProbe()
    queue, runner, _, decoy, _ = await _to_buffer_end_with_applied_decoy(tmp_path, monkeypatch, probe)
    probe.alerts = [_decoy_alarm(decoy), _decoy_alarm(decoy, service="commerce-somewhere-else")]

    state = await queue.tick()

    assert state.phase == "cycle_reset"
    assert "preflight not clean" in state.reason
    assert runner.started == []


@pytest.mark.asyncio
async def test_sweep_closes_lingering_incidents_when_only_decoy_alarms_fire(
    tmp_path: Path, monkeypatch
) -> None:
    """Before U7 a decoy's alarm counted as a second hard failure, so the sweep
    refused and the cycle restarted: the decoy was read as residue."""
    probe = DecoyAwareProbe(open_incidents=1)
    closed = []

    def close_open_incidents():
        closed.append(True)
        probe.open_incidents = 0
        return [{"id": "inc-1", "status": "open", "title": "t"}]

    monkeypatch.setattr("app.incident_close.close_open_incidents", close_open_incidents)
    queue, runner, _, decoy, _ = await _to_buffer_end_with_applied_decoy(tmp_path, monkeypatch, probe)
    probe.alerts = [_decoy_alarm(decoy)]

    state = await queue.tick()

    assert closed == [True]
    assert state.phase == "running"


@pytest.mark.asyncio
async def test_sweep_does_not_close_incidents_beside_a_real_alarm(tmp_path: Path, monkeypatch) -> None:
    probe = DecoyAwareProbe(open_incidents=1)
    closed = []
    monkeypatch.setattr(
        "app.incident_close.close_open_incidents", lambda: closed.append(True) or []
    )
    queue, runner, _, decoy, _ = await _to_buffer_end_with_applied_decoy(tmp_path, monkeypatch, probe)
    probe.alerts = [_decoy_alarm(decoy, service="commerce-somewhere-else")]

    state = await queue.tick()

    assert closed == []
    assert state.phase == "cycle_reset"


@pytest.mark.asyncio
async def test_a_decoy_alarm_still_firing_after_the_grace_blocks(tmp_path: Path, monkeypatch) -> None:
    """U7-review B2: with the shipped 15-minute grace, an alarm on the decoy's
    target that is still firing at the preflight (over 20 minutes after any
    near-fault decoy finished) is residue, not an excuse."""
    probe = DecoyAwareProbe()
    queue, runner, _, decoy, _ = await _to_buffer_end_with_applied_decoy(
        tmp_path, monkeypatch, probe, grace=900
    )
    probe.alerts = [_decoy_alarm(decoy)]

    state = await queue.tick()

    assert state.phase == "cycle_reset"
    assert runner.started == []


@pytest.mark.asyncio
async def test_a_rollback_still_rolling_at_buffer_end_restarts_the_cycle(
    tmp_path: Path, monkeypatch
) -> None:
    """U7-review: a decoy undone at the buffer starts a rollback nobody waited
    for. If it is still rolling when the buffer ends, the window is not clean."""
    seed = _seed_with(
        lambda records: bool(records) and records[-1].kind in {"restart", "config_rollout"}
    )
    executor = FakeDecoyExecutor(never_done={f"d{n}" for n in range(1, 10)})
    queue, runner, _, clock, _, _ = _decoy_queue(
        tmp_path, monkeypatch, seed=seed, probe=DecoyAwareProbe(), executor=executor
    )
    await queue.start()
    state = await queue.tick()
    last = state.cycle_decoys[-1]
    clock.value = _parse(last.planned_at)
    await queue.tick()  # last decoy starts rolling and never becomes healthy
    clock.value = state_expected(queue.snapshot())
    state = await queue.tick()  # -> buffer: one last check, then undo
    assert state.phase == "cycle_buffer"
    assert state.cycle_decoys[-1].status == "rolled_back"

    clock.value = state_expected(state)
    state = await queue.tick()

    assert state.phase == "cycle_reset"
    assert "rollback still rolling" in state.reason
    assert runner.started == []


@pytest.mark.asyncio
async def test_decoys_on_with_zero_drawn_still_records_an_empty_excuse_list(
    tmp_path: Path, monkeypatch
) -> None:
    seed = _seed_with(lambda records: records == [])
    probe = DecoyAwareProbe()
    queue, runner, _, clock, _, _ = _decoy_queue(tmp_path, monkeypatch, seed=seed, probe=probe)
    state = await _advance_to_injection(queue, runner, clock, scenario_id="F01-R")

    run_dir = runner.artifact_store.root / state.current_run_id
    assert json.loads((run_dir / "decoys.json").read_text())["decoys"] == []
    assert json.loads((run_dir / "preflight.json").read_text())["excused_alarms"] == []

# --- U1: cycle ticks keep the event loop free -------------------------------


class SlowRestoreRunner(FakeRestoreRunner):
    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay

    def __call__(self, cmd, **kwargs):
        import time

        time.sleep(self.delay)
        return super().__call__(cmd, **kwargs)


def _golden_queue(tmp_path: Path, monkeypatch, *, delay: float):
    monkeypatch.setenv("MODEL_SNAPSHOT_SSH_TARGET", "ydkim@192.168.230.119")
    queue, runner, coordinator, scheduler, clock = make_cycle_queue(tmp_path)
    ssh_key = queue.state_path.parent / "ssh_key"
    ssh_key.write_text("key", encoding="utf-8")
    queue.golden_reset_enabled = True
    queue.restore_retry_delay_sec = 0
    queue._restore_runner = SlowRestoreRunner(delay)  # type: ignore[assignment]
    queue.required_paths["ssh_key"] = ssh_key
    return queue, runner, coordinator, scheduler, clock


async def _count_beats_during(coro):
    import asyncio

    beats = 0

    async def ticker() -> None:
        nonlocal beats
        while True:
            await asyncio.sleep(0.02)
            beats += 1

    task = asyncio.create_task(ticker())
    try:
        result = await coro
    finally:
        task.cancel()
    return result, beats


@pytest.mark.asyncio
async def test_cycle_reset_golden_restore_runs_off_the_event_loop(tmp_path: Path, monkeypatch) -> None:
    """U1: cycle_reset restores the golden over ssh and thaws the trainer (up to
    900s + 60s). Inline that froze the runner's loop: the API went unanswered
    for minutes after a cycle restart (2026-10-12, 2026-10-22)."""
    import threading

    queue, runner, _, _, _ = _golden_queue(tmp_path, monkeypatch, delay=0.3)
    started_on: list[str] = []

    class Collector(FakeTopologyCollector):
        def start(self) -> None:
            started_on.append(threading.current_thread().name)
            super().start()

    queue.topology_collector_factory = lambda bundle_dir: Collector(bundle_dir)
    await queue.start()

    state, beats = await _count_beats_during(queue.tick())

    assert state.phase == "cycle_normal"
    assert beats >= 15, f"event loop was held during golden restore (beats={beats})"
    # The collector's task belongs to the loop, so its start is handed back there.
    assert started_on == [threading.main_thread().name]
    assert queue._topology_collector is not None and queue._topology_collector.started


@pytest.mark.asyncio
async def test_cycle_buffer_preflight_gate_runs_off_the_event_loop(tmp_path: Path) -> None:
    import time

    queue, runner, _, _, clock = make_cycle_queue(tmp_path)
    await queue.start()
    state = await queue.tick()  # -> cycle_normal
    clock.value = state_expected(state)
    state = await queue.tick()  # -> cycle_buffer
    clock.value = state_expected(state)

    def slow_preflight(now, waited_sec, **_gate):
        time.sleep(0.4)
        return SimpleNamespace(is_clean=True, verdict="clean", checks=[])

    queue.preflight_probe = object()  # type: ignore[assignment]
    queue._evaluate_preflight = slow_preflight  # type: ignore[method-assign]
    queue._write_preflight = lambda run_id, verdict: None  # type: ignore[method-assign]

    state, beats = await _count_beats_during(queue.tick())

    assert state.phase == "running"
    assert beats >= 10, f"event loop was held during the preflight gate (beats={beats})"


@pytest.mark.asyncio
async def test_api_answers_while_a_cycle_tick_holds_a_blocking_call(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    import time

    from httpx import ASGITransport, AsyncClient

    from app import main

    queue, runner, _, _, _ = _golden_queue(tmp_path, monkeypatch, delay=0.6)
    await queue.start()
    monkeypatch.setattr(main, "get_live_queue", lambda: queue)

    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://t") as client:
        began = time.monotonic()
        tick = asyncio.create_task(queue.tick())  # restore + thaw: about 1.2s
        await asyncio.sleep(0.1)
        health = await client.get("/healthz")
        live = await client.get("/api/live-queue")
        elapsed = time.monotonic() - began
        answered_mid_tick = not tick.done()
        state = await tick

    assert (health.status_code, live.status_code) == (200, 200)
    assert answered_mid_tick and elapsed < 0.5, (
        f"API waited {elapsed:.2f}s behind the cycle tick"
    )
    assert state.phase == "cycle_normal"


# --- U2: a restarted cycle waits out the previous run's effect ---------------


def _write_run_timeline(runner, run_id: str, *, applied_at: str, effect_ended_at: str | None) -> None:
    run_dir = runner.artifact_store.root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "timeline.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "level_changes": [
                    {"applied_at": applied_at, "effect_ended_at": effect_ended_at}
                ],
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_restart_after_an_injected_run_holds_the_normal_window_until_effect_end_plus_cooldown(
    tmp_path: Path,
) -> None:
    """U2: a cycle restart used to begin the next 2h normal window at once, with
    the abandoned run's recovery still echoing (F50-R dump discarded 2026-10-13).
    The window now starts no earlier than effect end + CYCLE_COOLDOWN (30m), the
    same settle a successful cycle gets after t2."""
    queue, runner, _, _, clock = make_cycle_queue(tmp_path)
    state = await _advance_to_injection(queue, runner, clock, scenario_id="F01-R")
    run_id = state.current_run_id
    _write_run_timeline(
        runner, run_id, applied_at="2026-07-16T10:10:30Z", effect_ended_at="2026-07-16T10:20:00Z"
    )
    runner.current = runner.current.model_copy(update={"status": "failed", "dirty": False})
    clock.value = datetime(2026, 7, 16, 10, 21, tzinfo=timezone.utc)

    state = await queue.tick()
    assert state.phase == "cycle_reset"
    assert state.expected_transition_at == "2026-07-16T10:50:00Z"
    assert "held until 2026-07-16T10:50:00Z" in state.reason

    clock.value = datetime(2026, 7, 16, 10, 49, 59, tzinfo=timezone.utc)
    state = await queue.tick()
    assert state.phase == "cycle_reset"  # still settling: no golden reset, no normal window

    clock.value = datetime(2026, 7, 16, 10, 50, tzinfo=timezone.utc)
    state = await queue.tick()
    assert state.phase == "cycle_normal"
    assert state.cycle_started_at == "2026-07-16T10:50:00Z"
    assert state.expected_transition_at == "2026-07-16T12:50:00Z"


@pytest.mark.asyncio
async def test_restart_of_a_run_that_applied_nothing_resets_at_once(tmp_path: Path) -> None:
    queue, runner, _, _, clock = make_cycle_queue(tmp_path)
    state = await _advance_to_injection(queue, runner, clock, scenario_id="F01-R")
    run_dir = runner.artifact_store.root / state.current_run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "state.json").write_text(json.dumps({"level_changes": []}), encoding="utf-8")
    runner.current = runner.current.model_copy(update={"status": "failed", "dirty": False})

    state = await queue.tick()
    assert state.phase == "cycle_reset"
    assert state.expected_transition_at == queue._format(clock.now())
    assert (await queue.tick()).phase == "cycle_normal"


@pytest.mark.asyncio
async def test_effect_still_open_at_restart_counts_from_the_restart(tmp_path: Path) -> None:
    queue, runner, _, _, clock = make_cycle_queue(tmp_path)
    state = await _advance_to_injection(queue, runner, clock, scenario_id="F01-R")
    _write_run_timeline(
        runner, state.current_run_id, applied_at="2026-07-16T10:10:30Z", effect_ended_at=None
    )
    runner.current = runner.current.model_copy(update={"status": "failed", "dirty": False})
    clock.value = datetime(2026, 7, 16, 10, 30, tzinfo=timezone.utc)

    state = await queue.tick()
    assert state.expected_transition_at == "2026-07-16T11:00:00Z"


@pytest.mark.asyncio
async def test_resume_after_a_cleaned_dirty_run_waits_out_the_cooldown(tmp_path: Path) -> None:
    """U2: F63-R 2026-10-26. A watchdog left the run DIRTY, a manual cleanup
    ended its effect at 06:44:09, resume at 06:49:28 started the baseline inside
    the tail. Resume now holds cycle_reset until effect end + 30m."""
    queue, runner, _, _, clock = make_cycle_queue(tmp_path)
    state = await _advance_to_injection(queue, runner, clock, scenario_id="F01-R")
    run_id = state.current_run_id
    _write_run_timeline(runner, run_id, applied_at="2026-07-16T10:10:30Z", effect_ended_at=None)
    runner.current = runner.current.model_copy(update={"status": "failed", "dirty": True})
    state = await queue.tick()
    assert state.phase == "paused"

    (runner.artifact_store.root / run_id / "cleanup.json").write_text(
        json.dumps({"succeeded": True, "effect_ended_at": "2026-07-16T10:20:00+00:00"}),
        encoding="utf-8",
    )
    clock.value = datetime(2026, 7, 16, 10, 25, tzinfo=timezone.utc)
    state = await queue.resume()
    assert state.phase == "cycle_reset"
    assert state.expected_transition_at == "2026-07-16T10:50:00Z"
    assert "held until 2026-07-16T10:50:00Z" in state.reason

    clock.value = datetime(2026, 7, 16, 10, 40, tzinfo=timezone.utc)
    assert (await queue.tick()).phase == "cycle_reset"
    clock.value = datetime(2026, 7, 16, 10, 50, tzinfo=timezone.utc)
    state = await queue.tick()
    assert state.phase == "cycle_normal"
    assert state.cycle_started_at == "2026-07-16T10:50:00Z"


# --- U3: why a run failed reaches the API and the log ------------------------


@pytest.mark.asyncio
async def test_restart_reason_carries_the_run_failure_text_into_api_and_log(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    """U3: 2026-10-22 F35-P restarted on a preflight refusal whose text never
    reached the queue: the reason named only the run, and cycle_reset cleared
    even that. The run's own reason (with the executor's stderr) now rides in
    the restart reason, stays through the normal window and is logged."""
    import logging

    from httpx import ASGITransport, AsyncClient

    from app import main

    detail = (
        "profile_apply_failed:CalledProcessError:profile control refused: profile preflight "
        "failed: tablespace of BANKING.ACCOUNTS has 267MB free, preflight needs at least 1024MB"
    )
    queue, runner, _, _, clock = make_cycle_queue(tmp_path)
    queue._controller_retry_reason = lambda run_id: detail  # type: ignore[method-assign]
    state = await _advance_to_injection(queue, runner, clock, scenario_id="F01-R")
    run_id = state.current_run_id
    runner.current = runner.current.model_copy(update={"status": "failed", "dirty": False})

    with caplog.at_level(logging.WARNING, logger="scenario_runner.live_queue"):
        state = await queue.tick()
    assert state.phase == "cycle_reset"
    assert run_id in state.reason and detail in state.reason
    assert any(detail in record.getMessage() for record in caplog.records)

    state = await queue.tick()  # nothing was applied: reset at once
    assert state.phase == "cycle_normal"
    assert detail in state.reason

    monkeypatch.setattr(main, "get_live_queue", lambda: queue)
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://t") as client:
        body = (await client.get("/api/live-queue")).json()
    assert detail in body["reason"]


# --- review 1st round ---------------------------------------------------------


@pytest.mark.asyncio
async def test_start_after_cancel_waits_out_the_last_runs_settle(tmp_path: Path) -> None:
    """Review R2: testbed-operate holds a scenario with cancel and starts the
    next. That start built cycle_reset at now and so opened the normal window
    inside the tail of the run just cleaned up by hand (F63-R 2026-10-26)."""
    queue, runner, _, _, clock = make_cycle_queue(tmp_path)
    state = await _advance_to_injection(queue, runner, clock, scenario_id="F01-R")
    run_id = state.current_run_id
    _write_run_timeline(runner, run_id, applied_at="2026-07-16T10:10:30Z", effect_ended_at=None)
    runner.current = runner.current.model_copy(update={"status": "failed", "dirty": True})
    assert (await queue.tick()).phase == "paused"
    (runner.artifact_store.root / run_id / "cleanup.json").write_text(
        json.dumps({"succeeded": True, "effect_ended_at": "2026-07-16T10:20:00+00:00"}),
        encoding="utf-8",
    )
    clock.value = datetime(2026, 7, 16, 10, 25, tzinfo=timezone.utc)
    await queue.cancel(reason="hold F01-R")

    state = await queue.start()

    assert state.phase == "cycle_reset"
    assert state.expected_transition_at == "2026-07-16T10:50:00Z"
    assert "held until 2026-07-16T10:50:00Z" in state.reason
    clock.value = datetime(2026, 7, 16, 10, 49, tzinfo=timezone.utc)
    assert (await queue.tick()).phase == "cycle_reset"
    clock.value = datetime(2026, 7, 16, 10, 50, tzinfo=timezone.utc)
    assert (await queue.tick()).phase == "cycle_normal"


@pytest.mark.asyncio
async def test_resume_keeps_a_settle_a_restart_had_already_scheduled(tmp_path: Path) -> None:
    """Review R2: _restart_cycle clears current_run_id. A pause while the reset
    is held, then resume, used to reset at once and lose the rest of the settle."""
    queue, runner, _, _, clock = make_cycle_queue(tmp_path)
    state = await _advance_to_injection(queue, runner, clock, scenario_id="F01-R")
    _write_run_timeline(
        runner, state.current_run_id,
        applied_at="2026-07-16T10:10:30Z", effect_ended_at="2026-07-16T10:20:00Z",
    )
    runner.current = runner.current.model_copy(update={"status": "failed", "dirty": False})
    clock.value = datetime(2026, 7, 16, 10, 21, tzinfo=timezone.utc)
    state = await queue.tick()
    assert state.expected_transition_at == "2026-07-16T10:50:00Z"
    assert state.current_run_id is None
    queue._pause(state, "operator halt")

    clock.value = datetime(2026, 7, 16, 10, 30, tzinfo=timezone.utc)
    resumed = await queue.resume()

    assert resumed.expected_transition_at == "2026-07-16T10:50:00Z"


@pytest.mark.asyncio
async def test_manual_run_is_refused_while_the_queue_restores_the_golden(
    tmp_path: Path, monkeypatch
) -> None:
    """Review R3: with the restore off the loop, a web run landed between the
    queue's busy check and its restore/injection."""
    import asyncio

    from httpx import ASGITransport, AsyncClient

    from app import main

    queue, runner, _, _, _ = _golden_queue(tmp_path, monkeypatch, delay=0.5)
    await queue.start()
    monkeypatch.setattr(main, "get_live_queue", lambda: queue)
    monkeypatch.setattr(main, "get_runner", lambda: runner)

    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://t") as client:
        tick = asyncio.create_task(queue.tick())
        await asyncio.sleep(0.1)
        response = await client.post("/api/scenarios/F01-R/run")
        state = await tick

    assert response.status_code == 409
    assert "live queue" in response.json()["detail"]
    assert runner.started == []
    assert state.phase == "cycle_normal"
    assert queue.injection_reserved is False


@pytest.mark.asyncio
async def test_manual_run_is_refused_between_the_buffer_gate_and_the_injection(
    tmp_path: Path, monkeypatch
) -> None:
    import asyncio
    import time

    from httpx import ASGITransport, AsyncClient

    from app import main

    queue, runner, _, _, clock = make_cycle_queue(tmp_path)
    await queue.start()
    state = await queue.tick()
    clock.value = state_expected(state)
    state = await queue.tick()  # -> cycle_buffer
    clock.value = state_expected(state)

    def slow_preflight(now, waited_sec, **_gate):
        time.sleep(0.4)
        return SimpleNamespace(is_clean=True, verdict="clean", checks=[])

    queue.preflight_probe = object()  # type: ignore[assignment]
    queue._evaluate_preflight = slow_preflight  # type: ignore[method-assign]
    queue._write_preflight = lambda run_id, verdict: None  # type: ignore[method-assign]
    monkeypatch.setattr(main, "get_live_queue", lambda: queue)
    monkeypatch.setattr(main, "get_runner", lambda: runner)

    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://t") as client:
        tick = asyncio.create_task(queue.tick())
        await asyncio.sleep(0.1)
        response = await client.post("/api/scenarios/F01-H/run")
        state = await tick

    assert response.status_code == 409
    assert runner.started == [("F01-R", "run")]  # only the queue's own injection
    assert state.phase == "running"


@pytest.mark.asyncio
async def test_worker_failure_pause_runs_off_the_event_loop(tmp_path: Path, monkeypatch) -> None:
    """Review residual: the worker's failure pause thaws the trainer over ssh."""
    queue, runner, _, _, _ = _golden_queue(tmp_path, monkeypatch, delay=0.4)
    await queue.start()
    queue.poll_interval_sec = 0

    async def failing_tick():
        raise RuntimeError("boom")

    queue.tick = failing_tick  # type: ignore[method-assign]

    _, beats = await _count_beats_during(queue._loop())

    assert queue.snapshot().phase == "paused"
    assert "queue worker failed" in queue.snapshot().reason
    assert beats >= 10, f"event loop was held during the failure pause (beats={beats})"


@pytest.mark.asyncio
async def test_failed_collector_start_unbinds_the_collector(tmp_path: Path) -> None:
    """Review residual: a start that fails on the loop must not leave an empty
    bundle bound to the next run."""

    class BrokenCollector(FakeTopologyCollector):
        def start(self) -> None:
            raise RuntimeError("no loop")

    queue, runner, _, _, clock = make_cycle_queue(tmp_path)
    queue.topology_collector_factory = lambda bundle_dir: BrokenCollector(bundle_dir)
    await queue.start()
    state = await queue.tick()  # cycle_reset on a worker thread, start on the loop
    assert state.phase == "cycle_normal"
    assert queue._topology_collector is None


@pytest.mark.asyncio
async def test_queue_waits_while_a_manual_run_prepares_its_capsule(tmp_path: Path, monkeypatch) -> None:
    """Review 2 N1 (the reverse of R3): a manual run preparing its capsule off
    the loop has passed the runner's gate but holds neither a run nor a lease.
    The queue's busy check did not see it and restored the golden meanwhile."""
    import asyncio
    import threading

    from app.coordinator import GlobalCoordinator
    from app.runner import ScenarioRunner
    from tests.test_controller_orchestration import (
        _controller_yaml,
        _runner_scenario,
        parse_controller,
    )

    script = tmp_path / "scenario.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    scenario = _runner_scenario(
        script, controller=parse_controller(_controller_yaml(mode="evaluation"))
    ).model_copy(update={"injection": {"profile_id": "p", "catalog_slug": "s"}})
    monkeypatch.setattr("app.runner.get_scenario", lambda _: scenario)
    real = ScenarioRunner(
        tmp_path, tmp_path / "logs", coordinator=GlobalCoordinator(tmp_path / "state.json")
    )
    release = threading.Event()
    entered = threading.Event()

    def blocking_prepare(run_id, **kwargs):
        entered.set()
        release.wait(5)
        raise RuntimeError("prepare refused")

    monkeypatch.setattr(real.artifact_store, "prepare_capsule", blocking_prepare)

    (tmp_path / "q").mkdir()
    queue, runner, _, _, _ = _golden_queue(tmp_path / "q", monkeypatch, delay=0)
    # The queue sees the real runner's start state through the same attribute.
    type(runner).is_starting = property(lambda self: real.is_starting)
    try:
        await queue.start()
        manual = asyncio.create_task(real.start(scenario.id, "run"))
        while not entered.is_set():
            await asyncio.sleep(0.01)

        state = await queue.tick()

        assert state.phase == "cycle_reset"
        assert queue._restore_runner.remotes == []  # no golden restore meanwhile
        release.set()
        with pytest.raises(RuntimeError, match="prepare refused"):
            await manual
        state = await queue.tick()
        assert state.phase == "cycle_normal"
    finally:
        del type(runner).is_starting


@pytest.mark.asyncio
async def test_manual_cleanup_is_refused_while_the_queue_restores_the_golden(
    tmp_path: Path, monkeypatch
) -> None:
    import asyncio

    from httpx import ASGITransport, AsyncClient

    from app import main

    queue, runner, _, _, _ = _golden_queue(tmp_path, monkeypatch, delay=0.5)
    await queue.start()
    monkeypatch.setattr(main, "get_live_queue", lambda: queue)
    monkeypatch.setattr(main, "get_runner", lambda: runner)

    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://t") as client:
        tick = asyncio.create_task(queue.tick())
        await asyncio.sleep(0.1)
        response = await client.post("/api/scenarios/F01-R/cleanup")
        await tick

    assert response.status_code == 409
    assert runner.started == []
