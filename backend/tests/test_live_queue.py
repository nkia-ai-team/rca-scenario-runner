from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.live_queue import LIVE_SCENARIO_ORDER, LiveScenarioQueue
from app.models import RunInfo


NOW = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)


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
        self.jobs = {}
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
        self.started = []
        self.capture_worker_starts = 0

    async def start(self, *, scenario_id: str, mode: str):
        self.started.append((scenario_id, mode))
        self.current = RunInfo(
            run_id=f"run-{scenario_id}",
            scenario_id=scenario_id,
            mode="run",
            status="running",
            started_at=NOW,
        )
        return self.current

    def get_current(self):
        return self.current

    def ensure_capture_worker(self) -> None:
        self.capture_worker_starts += 1


def make_queue(tmp_path: Path):
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
                "scenarios": {
                    scenario_id: metadata
                    for scenario_id in [*LIVE_SCENARIO_ORDER, "F05-G", "F12-H"]
                },
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
    )
    queue.ensure_worker = lambda: None  # type: ignore[method-assign]
    queue._controller_evidence_error = lambda _: None  # type: ignore[method-assign]
    return queue, runner, coordinator, scheduler, clock


@pytest.mark.asyncio
async def test_fixed_sequence_waits_for_capture_and_two_hour_clean_window(tmp_path: Path) -> None:
    queue, runner, _, scheduler, clock = make_queue(tmp_path)
    state = await queue.start()
    assert state.scenario_ids == list(LIVE_SCENARIO_ORDER)

    state = await queue.tick()
    assert runner.started == [("F01-R", "run")]
    run_id = state.current_run_id
    assert run_id == "run-F01-R"

    runner.current = runner.current.model_copy(
        update={"status": "succeeded", "finished_at": clock.now(), "exit_code": 0}
    )
    scheduler.jobs[run_id] = SimpleNamespace(
        status="pending", t2="2026-07-16T08:10:00Z", failure=None
    )
    state = await queue.tick()
    assert state.phase == "waiting_capture"

    scheduler.jobs[run_id].status = "completed"
    capture = runner.artifact_store.root / run_id / "capture-complete.json"
    capture.parent.mkdir(parents=True)
    capture.write_text("{}\n", encoding="utf-8")
    runner.current = None
    state = await queue.tick()
    assert state.phase == "waiting_clean_window"
    assert state.first_gate_passed is True
    assert state.next_index == 1
    assert state.clean_window_not_before == "2026-07-16T08:40:00Z"

    clock.value = datetime(2026, 7, 16, 8, 39, 59, tzinfo=timezone.utc)
    assert (await queue.tick()).phase == "waiting_clean_window"
    clock.value += timedelta(seconds=1)
    assert (await queue.tick()).phase == "running"
    await queue.tick()
    assert runner.started[-1] == ("F01-H", "run")


@pytest.mark.asyncio
async def test_capture_failure_pauses_and_resume_retries_same_scenario(tmp_path: Path) -> None:
    queue, runner, _, scheduler, _ = make_queue(tmp_path)
    await queue.start()
    state = await queue.tick()
    run_id = state.current_run_id
    runner.current = runner.current.model_copy(update={"status": "succeeded", "exit_code": 0})
    scheduler.jobs[run_id] = SimpleNamespace(
        status="pending", t2="2026-07-16T08:10:00Z", failure=None
    )
    await queue.tick()
    scheduler.jobs[run_id].status = "failed"
    scheduler.jobs[run_id].failure = "postgres unavailable"
    state = await queue.tick()
    assert state.phase == "paused"
    assert "capture failed" in state.reason
    assert state.next_index == 0

    runner.current = None
    state = await queue.resume()
    assert state.phase == "waiting_clean_window"
    clock = queue.clock
    clock.value = datetime(2026, 7, 16, 10, 10, tzinfo=timezone.utc)
    assert (await queue.tick()).phase == "running"
    await queue.tick()
    assert runner.started == [("F01-R", "run"), ("F01-R", "run")]


@pytest.mark.asyncio
async def test_transient_controller_failure_auto_retries_after_clean_window_without_capture(
    tmp_path: Path,
) -> None:
    queue, runner, _, _, clock = make_queue(tmp_path)
    await queue.start()
    state = await queue.tick()
    runner.current = runner.current.model_copy(update={"status": "succeeded", "exit_code": 0})
    queue._controller_evidence_error = lambda _: "controller aborted"  # type: ignore[method-assign]
    queue._controller_retry_reason = (  # type: ignore[method-assign]
        lambda _: "safety_observation_unavailable"
    )
    queue._previous_t2 = (  # type: ignore[method-assign]
        lambda _: datetime(2026, 7, 16, 8, 10, tzinfo=timezone.utc)
    )

    state = await queue.tick()

    assert state.phase == "waiting_clean_window"
    assert state.next_index == 0
    assert state.auto_retry_counts == {"F01-R": 1}
    assert state.pending_retry_capture_run_id is None
    assert state.clean_window_not_before == "2026-07-16T08:40:00Z"

    clock.value = datetime(2026, 7, 16, 8, 40, tzinfo=timezone.utc)
    state = await queue.tick()
    assert state.phase == "running"
    assert state.pending_retry_capture_run_id is None


@pytest.mark.asyncio
async def test_transient_controller_failure_stops_after_auto_retry_budget(tmp_path: Path) -> None:
    queue, runner, _, scheduler, _ = make_queue(tmp_path)
    await queue.start()
    state = await queue.tick()
    run_id = state.current_run_id
    runner.current = runner.current.model_copy(update={"status": "succeeded", "exit_code": 0})
    scheduler.jobs[run_id] = SimpleNamespace(
        run_id=run_id,
        status="pending",
        t2="2026-07-16T08:10:00Z",
        failure=None,
    )
    queue._controller_evidence_error = lambda _: "controller aborted"  # type: ignore[method-assign]
    queue._controller_retry_reason = (  # type: ignore[method-assign]
        lambda _: "safety_observation_unavailable"
    )
    state = state.model_copy(update={"auto_retry_counts": {"F01-R": 2}})
    queue._write(state)

    state = await queue.tick()

    assert state.phase == "paused"
    assert state.auto_retry_counts == {"F01-R": 2}


@pytest.mark.asyncio
async def test_resume_preserves_pending_capture_and_restarts_capture_worker(tmp_path: Path) -> None:
    queue, runner, _, scheduler, _ = make_queue(tmp_path)
    await queue.start()
    state = await queue.tick()
    run_id = state.current_run_id
    runner.current = runner.current.model_copy(update={"status": "succeeded", "exit_code": 0})
    scheduler.jobs[run_id] = SimpleNamespace(
        status="pending", t2="2026-07-16T08:10:00Z", failure=None
    )
    state = await queue.tick()
    assert state.phase == "waiting_capture"
    state = queue._pause(state, "transient worker validation error")

    runner.current = None
    state = await queue.resume()

    assert state.phase == "waiting_capture"
    assert state.current_scenario_id == "F01-R"
    assert state.current_run_id == run_id
    assert state.next_index == 0
    assert runner.capture_worker_starts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("capture_status", ["pending", "model_requested", "completed"])
async def test_resume_reruns_scenario_when_capture_exists_but_controller_failed(
    tmp_path: Path,
    capture_status: str,
) -> None:
    queue, runner, _, scheduler, clock = make_queue(tmp_path)
    await queue.start()
    state = await queue.tick()
    run_id = state.current_run_id
    runner.current = runner.current.model_copy(update={"status": "succeeded", "exit_code": 0})
    scheduler.jobs[run_id] = SimpleNamespace(
        status=capture_status, t2="2026-07-16T08:10:00Z", failure=None
    )
    state = queue._pause(state, "controller success evidence failed: aborted")
    queue._controller_evidence_error = lambda _: "controller aborted"  # type: ignore[method-assign]

    runner.current = None
    state = await queue.resume()

    assert state.phase == "waiting_clean_window"
    assert state.next_index == 0
    assert state.current_run_id is None
    clock.value = datetime(2026, 7, 16, 10, 10, tzinfo=timezone.utc)
    assert (await queue.tick()).phase == "running"
    await queue.tick()
    assert runner.started == [("F01-R", "run"), ("F01-R", "run")]


@pytest.mark.asyncio
async def test_dirty_state_stops_queue_and_persists_across_restart(tmp_path: Path) -> None:
    queue, runner, coordinator, _, clock = make_queue(tmp_path)
    await queue.start()
    coordinator.dirty_run = SimpleNamespace(run_id="dirty-1")
    state = await queue.tick()
    assert state.phase == "paused"
    assert "DIRTY" in state.reason

    restored = LiveScenarioQueue(
        runner,  # type: ignore[arg-type]
        queue.state_path,
        clock=clock,
        required_paths={},
        required_env=(),
    )
    assert restored.snapshot() == state


@pytest.mark.asyncio
async def test_readiness_blocks_before_any_start(tmp_path: Path) -> None:
    queue, runner, _, _, _ = make_queue(tmp_path)
    queue.required_paths = {"missing-key": tmp_path / "missing"}
    with pytest.raises(RuntimeError, match="missing-key"):
        await queue.start()
    assert runner.started == []


@pytest.mark.asyncio
async def test_new_live_ids_append_only_to_running_frozen_queue(tmp_path: Path) -> None:
    queue, runner, _, _, clock = make_queue(tmp_path)
    registry = tmp_path / "controllers.json"
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    promoted = [*LIVE_SCENARIO_ORDER, "F05-G"]
    registry.write_text(json.dumps({"live_scenario_ids": promoted}), encoding="utf-8")
    for scenario_id in [*promoted, "F12-H"]:
        (manifests / f"{scenario_id.lower()}.yaml").write_text(
            json.dumps(
                {
                    "id": scenario_id,
                    "execution": {"controller": {"live_enabled": True}},
                }
            ),
            encoding="utf-8",
        )
    queue.scenario_registry_path = registry
    queue.manifest_root = manifests
    state = await queue.start()
    assert state.scenario_ids == promoted
    frozen_digest = state.catalog_snapshot_sha256

    registry.write_text(
        json.dumps({"live_scenario_ids": [*promoted, "F12-H"]}), encoding="utf-8"
    )
    appended = await queue.append_promoted()
    assert appended.scenario_ids == [*promoted, "F12-H"]
    assert appended.catalog_snapshot_sha256 != frozen_digest

    restored = LiveScenarioQueue(
        runner,  # type: ignore[arg-type]
        queue.state_path,
        clock=clock,
        required_paths={},
        required_env=(),
        scenario_registry_path=registry,
        manifest_root=manifests,
    )
    assert restored.snapshot().scenario_ids == [*promoted, "F12-H"]
    assert restored.snapshot().catalog_snapshot_sha256 == appended.catalog_snapshot_sha256


@pytest.mark.asyncio
async def test_tick_automatically_appends_new_live_ids(tmp_path: Path) -> None:
    queue, _, _, _, _ = make_queue(tmp_path)
    registry = tmp_path / "controllers.json"
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    promoted = [*LIVE_SCENARIO_ORDER, "F05-G"]
    registry.write_text(json.dumps({"live_scenario_ids": promoted}), encoding="utf-8")
    for scenario_id in [*promoted, "F12-H"]:
        (manifests / f"{scenario_id.lower()}.yaml").write_text(
            json.dumps(
                {
                    "id": scenario_id,
                    "execution": {"controller": {"live_enabled": True}},
                }
            ),
            encoding="utf-8",
        )
    queue.scenario_registry_path = registry
    queue.manifest_root = manifests
    await queue.start()

    registry.write_text(
        json.dumps({"live_scenario_ids": [*promoted, "F12-H"]}), encoding="utf-8"
    )

    state = await queue.tick()
    assert state.scenario_ids == [*promoted, "F12-H"]
    assert state.current_scenario_id == "F01-R"


@pytest.mark.asyncio
async def test_tick_retries_incomplete_promotion_snapshot_without_pausing(tmp_path: Path) -> None:
    queue, _, _, _, _ = make_queue(tmp_path)
    registry = tmp_path / "controllers.json"
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    promoted = [*LIVE_SCENARIO_ORDER, "F05-G"]
    registry.write_text(json.dumps({"live_scenario_ids": promoted}), encoding="utf-8")
    for scenario_id in promoted:
        (manifests / f"{scenario_id.lower()}.yaml").write_text(
            json.dumps(
                {
                    "id": scenario_id,
                    "execution": {"controller": {"live_enabled": True}},
                }
            ),
            encoding="utf-8",
        )
    queue.scenario_registry_path = registry
    queue.manifest_root = manifests
    await queue.start()

    registry.write_text(
        json.dumps({"live_scenario_ids": [*promoted, "F12-H"]}), encoding="utf-8"
    )

    state = await queue.tick()
    assert state.phase == "running"
    assert state.scenario_ids == promoted


class FakePreflightProbe:
    """Return a fixed R6 signal snapshot; mutate `.obs` to model recovery."""

    CLEAN = {
        "baseline_loadgen_alive": 1,
        "achieved_rps": 8,
        "diurnal_rps_floor": 5,
        "user_5xx_rate": 0.0,
        "entry_p95_sec": 0.3,
        "active_alarms": 0,
        "open_incidents": 0,
        "prev_pool_pending": 0,
    }
    DIRTY = {**CLEAN, "active_alarms": 1}  # hard fail (eq 0)

    def __init__(self, obs: dict) -> None:
        self.obs = dict(obs)
        self.calls = 0

    def collect(self, *, now):
        self.calls += 1
        return dict(self.obs)


@pytest.mark.asyncio
async def test_clean_preflight_injects_and_drops_verdict_file(tmp_path: Path) -> None:
    queue, runner, _, _, _ = make_queue(tmp_path)
    queue.preflight_probe = FakePreflightProbe(FakePreflightProbe.CLEAN)
    await queue.start()

    state = await queue.tick()
    assert runner.started == [("F01-R", "run")]
    assert state.current_run_id == "run-F01-R"
    assert state.preflight_attempts == 0

    verdict = json.loads(
        (runner.artifact_store.root / "run-F01-R" / "preflight.json").read_text()
    )
    assert verdict["verdict"] == "clean"
    assert verdict["window"][1].endswith("Z")
    assert {c["name"] for c in verdict["checks"]} >= {
        "baseline_loadgen_alive", "diurnal_band_rps", "user_5xx_rate",
        "entry_p95_sec", "active_alarms", "open_incidents", "prev_pool_pending",
    }


@pytest.mark.asyncio
async def test_dirty_preflight_rechecks_every_five_minutes_then_skips(tmp_path: Path) -> None:
    queue, runner, _, _, clock = make_queue(tmp_path)
    queue.preflight_probe = FakePreflightProbe(FakePreflightProbe.DIRTY)
    await queue.start()

    state = await queue.tick()  # attempt 1: dirty -> wait
    assert runner.started == []
    assert state.phase == "running"
    assert state.preflight_attempts == 1
    assert state.preflight_retry_not_before is not None

    state = await queue.tick()  # before recheck interval: no re-evaluation
    assert state.preflight_attempts == 1

    clock.value += timedelta(minutes=5)
    state = await queue.tick()  # attempt 2
    assert state.preflight_attempts == 2

    clock.value += timedelta(minutes=5)
    state = await queue.tick()  # attempt 3 == MAX -> skip and advance
    assert runner.started == []
    assert state.skipped_scenario_ids == ["F01-R"]
    assert state.next_index == 1
    assert state.preflight_attempts == 0


@pytest.mark.asyncio
async def test_preflight_clears_on_recheck_and_records_clean_after_wait(tmp_path: Path) -> None:
    queue, runner, _, _, clock = make_queue(tmp_path)
    probe = FakePreflightProbe(FakePreflightProbe.DIRTY)
    queue.preflight_probe = probe
    await queue.start()

    state = await queue.tick()  # dirty -> wait
    assert state.preflight_attempts == 1
    assert runner.started == []

    probe.obs = dict(FakePreflightProbe.CLEAN)  # environment recovers
    clock.value += timedelta(minutes=5)
    state = await queue.tick()  # clean now -> inject
    assert runner.started == [("F01-R", "run")]

    verdict = json.loads(
        (runner.artifact_store.root / "run-F01-R" / "preflight.json").read_text()
    )
    assert verdict["verdict"] == "clean_after_wait"
    assert verdict["waited_sec"] == 300


@pytest.mark.asyncio
async def test_preflight_probe_failure_pauses_the_queue(tmp_path: Path) -> None:
    class BrokenProbe:
        def collect(self, *, now):
            return {"baseline_loadgen_alive": 1}  # missing required signals

    queue, runner, _, _, _ = make_queue(tmp_path)
    queue.preflight_probe = BrokenProbe()
    await queue.start()

    state = await queue.tick()
    assert state.phase == "paused"
    assert "preflight probe failed" in (state.reason or "")
    assert runner.started == []
