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


def make_cycle_queue(tmp_path: Path, *, cycle_ids=CYCLE_IDS, health_probe=None):
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
    )
    queue.ensure_worker = lambda: None  # type: ignore[method-assign]
    queue._controller_evidence_error = lambda _: None  # type: ignore[method-assign]
    return queue, runner, coordinator, scheduler, clock


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

    clock.value = state_expected(state)
    state = await queue.tick()  # cooldown -> waiting_capture (+phases.json)
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
    assert set(phases["trainer_reset"]) == {"phase", "at", "golden_id", "golden_sha256"}
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
    def __init__(self) -> None:
        self.remotes: list[str] = []

    def __call__(self, cmd, **kwargs):
        self.remotes.append(cmd[-1])
        return subprocess.CompletedProcess(cmd, 0, "", "")


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
