from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from app.live_queue import (
    ARCHIVE_STATE_CHECK_NAME,
    DOMAIN_PODS_CHECK_NAME,
    LIVE_SCENARIO_ORDER,
    PREFLIGHT_MAX_ATTEMPTS,
    ORACLE_CHECK_MAX_AGE,
    ORACLE_CHECK_NAME,
    READINESS_PROBE_GRACE,
    LiveScenarioQueue,
)
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
        self.skipped_isolation = []
        self.capture_worker_starts = 0

    async def start(self, *, scenario_id: str, mode: str, skip_isolation_checks: bool = False):
        self.started.append((scenario_id, mode))
        self.skipped_isolation.append(skip_isolation_checks)
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
async def test_capture_off_advances_without_an_export(tmp_path: Path, monkeypatch) -> None:
    """A smoke pass must not spend the whole post-judging tail on a discarded case.

    In a smoke pass no capture job is ever scheduled, so the queue has to advance
    on the run's own evidence. Waiting for a job that will never appear — or
    pausing on "capture was not scheduled" — stops the batch on every scenario.
    """
    monkeypatch.setenv("SCENARIO_PASS", "smoke")
    queue, runner, _, scheduler, clock = make_queue(tmp_path)
    await queue.start()

    state = await queue.tick()
    run_id = state.current_run_id
    runner.current = runner.current.model_copy(
        update={"status": "succeeded", "finished_at": clock.now(), "exit_code": 0}
    )
    # no scheduler.jobs entry, and no capture-complete.json on disk
    assert run_id not in scheduler.jobs
    state = await queue.tick()
    assert state.phase == "waiting_capture"

    runner.current = None
    state = await queue.tick()
    assert state.next_index == 1
    assert state.completed_run_ids == [run_id]
    assert state.reason is None


@pytest.mark.asyncio
async def test_smoke_pass_releases_the_capture_window_isolation_gates(
    tmp_path: Path, monkeypatch
) -> None:
    """The gates enforce capture-window separation, so they must follow the pass.

    They are the runner's own eligibility checks, independent of the queue's
    gap: on 2026-07-31 F06-R was blocked with check_failed:clean-window +
    scenario_overlap 15m after the previous t2, while the queue believed it had
    waited long enough. A pass that captures nothing has no windows to separate,
    and the preflight gate — which measures cleanliness instead of assuming a
    gap — still runs either way.
    """
    monkeypatch.setenv("SCENARIO_PASS", "smoke")
    queue, runner, _, _, _ = make_queue(tmp_path)
    await queue.start()
    await queue.tick()
    assert runner.skipped_isolation == [True]

    monkeypatch.setenv("SCENARIO_PASS", "dataset")
    (tmp_path / "dataset").mkdir()
    queue2, runner2, _, _, _ = make_queue(tmp_path / "dataset")
    await queue2.start()
    await queue2.tick()
    assert runner2.skipped_isolation == [False]


@pytest.mark.asyncio
async def test_every_run_records_which_batch_it_belongs_to(tmp_path: Path) -> None:
    """배치 소속은 런 자신의 아티팩트에 남아야 한다.

    큐 상태는 한 번에 한 배치만 들고 있어서, 다음 배치가 시작되면 이전 런의 소속이
    사라진다. 그러면 배치 비교가 타임스탬프 추측이 되는데, 2026-08-09 에 세 가지
    추측이 모두 틀렸다 — "최근 N개"는 배치 밖 런을 끌어왔고, 1시간 공백 탐지는
    ~13분짜리 배치 간 간격을 못 봤고, 고정 크기 윈도는 배치 안 재시도(F20-R 이 한
    배치에서 세 번 실행) 때문에 어긋났다.
    """
    queue, runner, _, _, _ = make_queue(tmp_path)
    state = await queue.start()
    state = await queue.tick()
    run_id = state.current_run_id

    marker = runner.artifact_store.root / run_id / "batch.json"
    assert marker.is_file(), "런이 자기 배치 소속을 기록하지 않았다"
    recorded = json.loads(marker.read_text(encoding="utf-8"))
    assert recorded["queue_id"] == state.queue_id
    assert recorded["index"] == 0
    assert recorded["started_at"] == state.started_at


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
    capture.parent.mkdir(parents=True, exist_ok=True)
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
    # The wait reason names the failing check and its value, not only "(dirty)".
    assert state.reason == (
        f"preflight not clean (dirty): active_alarms=1 (fail, eq 0); "
        f"recheck 1/{PREFLIGHT_MAX_ATTEMPTS}"
    )

    state = await queue.tick()  # before recheck interval: no re-evaluation
    assert state.preflight_attempts == 1

    # Attempts 2..MAX-1 keep waiting: a dirty window after a destructive
    # predecessor refills on its own, so the gate waits rather than forfeiting
    # the case. Only the last attempt gives up.
    for expected in range(2, PREFLIGHT_MAX_ATTEMPTS):
        clock.value += timedelta(minutes=5)
        state = await queue.tick()
        assert state.preflight_attempts == expected
        assert state.skipped_scenario_ids == []

    clock.value += timedelta(minutes=5)
    state = await queue.tick()  # attempt == MAX -> skip and advance
    assert runner.started == []
    assert state.skipped_scenario_ids == ["F01-R"]
    assert state.next_index == 1
    assert state.preflight_attempts == 0
    assert state.reason == (
        "preflight timeout: skipped F01-R (dirty): active_alarms=1 (fail, eq 0)"
    )


@pytest.mark.asyncio
async def test_v2_wait_and_skip_reasons_carry_a_refused_incident_sweep(
    tmp_path: Path, monkeypatch
) -> None:
    """Review K3SEP M1: the v2 gate's wait and skip reasons are where an
    operator reads why a scenario never injected; a refused sweep must show."""
    from app.incident_close import IncidentCloseError

    def refuse():
        raise IncidentCloseError(
            closed=[],
            failed=[{"id": "inc-1", "status": "active", "title": "t",
                     "error": 'HTTP 403 {"message":"forbidden origin"}'}],
        )

    monkeypatch.setattr("app.incident_close.close_open_incidents", refuse)
    queue, runner, _, _, clock = make_queue(tmp_path)
    queue.preflight_probe = FakePreflightProbe(
        {**FakePreflightProbe.CLEAN, "open_incidents": 3}
    )
    await queue.start()

    state = await queue.tick()
    assert runner.started == []
    assert state.reason.startswith(
        "preflight not clean (dirty): open_incidents=3 (fail, eq 0); "
        "incident sweep failed: IncidentCloseError: incident close failed for 1 of 1"
    )
    assert "forbidden origin" in state.reason
    assert state.reason.endswith(f"; recheck 1/{PREFLIGHT_MAX_ATTEMPTS}")
    assert json.loads(queue.state_path.read_text())["reason"] == state.reason

    for _ in range(2, PREFLIGHT_MAX_ATTEMPTS + 1):
        clock.value += timedelta(minutes=5)
        state = await queue.tick()
    assert state.skipped_scenario_ids == ["F01-R"]
    assert state.reason.startswith(
        "preflight timeout: skipped F01-R (dirty): open_incidents=3 (fail, eq 0); "
        "incident sweep failed: IncidentCloseError"
    )
    assert "forbidden origin" in state.reason


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
async def test_preflight_probe_failure_spends_the_recheck_budget_before_pausing(
    tmp_path: Path,
) -> None:
    """A raising probe is transport, not a verdict (2026-08-04 batch).

    119 answers this gate and resets connections under load; the batch used to
    die on the first blip while a merely dirty verdict got 6 x 5m of rechecks.
    Same budget for both, and a probe still broken at the end still pauses.
    """
    class BrokenProbe:
        def collect(self, *, now):
            raise ConnectionResetError(104, "Connection reset by peer")

    queue, runner, _, _, clock = make_queue(tmp_path)
    queue.preflight_probe = FakePreflightProbe(FakePreflightProbe.CLEAN)
    await queue.start()
    queue.preflight_probe = BrokenProbe()

    for attempt in range(1, PREFLIGHT_MAX_ATTEMPTS):
        state = await queue.tick()
        assert state.phase == "running", f"blip {attempt} must not end the batch"
        assert state.preflight_attempts == attempt
        assert "Connection reset by peer" in (state.reason or "")
        clock.value += timedelta(minutes=5)

    state = await queue.tick()
    assert state.phase == "paused", "a probe still broken after the budget pauses"
    assert "preflight probe failed" in (state.reason or "")
    assert runner.started == []


def _wait_in_clean_window(queue, clock) -> None:
    """Park the queue on the between-scenarios tick that re-probes readiness."""
    queue._write(
        queue.snapshot().model_copy(
            update={
                "phase": "waiting_clean_window",
                "clean_window_not_before": queue._format(
                    clock.value - timedelta(minutes=1)
                ),
            }
        )
    )


@pytest.mark.asyncio
async def test_readiness_probe_hiccup_waits_out_the_grace_and_records_why(
    tmp_path: Path, monkeypatch
) -> None:
    """The batch-level gate must survive a partial poll, and say what broke.

    Every other queue test runs with functional_readiness_enabled False, so the
    readiness gate — the layer that actually paused the 2026-08-04 batch — was
    never exercised: production pauses there before the per-scenario gate is
    reached. Enable it here and use the production failure shape (a partial poll,
    which build_preflight_checks raises on by design), on the clean-window tick
    between two scenarios — where the batch actually stopped.
    """
    class PartialProbe:
        def collect(self, *, now):
            return {"baseline_loadgen_alive": 1.0}  # the other 7 signals missing

    script = tmp_path / "capture.sh"
    script.write_text("#!/usr/bin/env bash\n[[ \"$1\" == --capabilities ]] && { echo \"capabilities: scenario-doc-file source-commits-json capture-script-meta\"; exit 0; }\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.setattr("app.incident_close.open_incident_count", lambda **kw: 0)

    queue, runner, _, _, clock = make_queue(tmp_path)
    queue.required_paths = {**queue.required_paths, "capture_script": script}
    queue.preflight_probe = FakePreflightProbe(FakePreflightProbe.CLEAN)
    await queue.start()
    queue.functional_readiness_enabled = True
    _wait_in_clean_window(queue, clock)

    queue.preflight_probe = PartialProbe()
    queue._functional_cache = None  # the green TTL has expired
    state = await queue.tick()
    assert state.phase == "waiting_clean_window", (
        "a single partial poll must not end the batch"
    )
    assert runner.started == []

    clock.value += READINESS_PROBE_GRACE + timedelta(seconds=5)
    state = await queue.tick()
    assert state.phase == "paused", "a probe broken past the grace must pause"
    assert "preflight_signals" in (state.reason or "")
    # The point of the fix: the reason carries the swallowed exception.
    assert "missing required signals" in (state.reason or "")


@pytest.mark.asyncio
async def test_readiness_pauses_at_once_when_a_non_transient_check_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """The grace is for the probes — a hard dependency failure still pauses now."""
    script = tmp_path / "capture.sh"
    script.write_text("#!/usr/bin/env bash\n[[ \"$1\" == --capabilities ]] && { echo \"capabilities: scenario-doc-file source-commits-json capture-script-meta\"; exit 0; }\nexit 0\n")
    script.chmod(0o755)

    def broken(**kwargs):
        raise RuntimeError("incident api down")

    monkeypatch.setattr("app.incident_close.open_incident_count", broken)

    queue, runner, _, _, clock = make_queue(tmp_path)
    queue.required_paths = {**queue.required_paths, "capture_script": script}
    queue.preflight_probe = FakePreflightProbe(FakePreflightProbe.CLEAN)
    await queue.start()
    queue.functional_readiness_enabled = True
    _wait_in_clean_window(queue, clock)
    queue._functional_cache = None

    state = await queue.tick()
    assert state.phase == "paused"
    assert "lucida_incident_api" in (state.reason or "")
    assert "incident api down" in (state.reason or "")
    assert runner.started == []


@pytest.mark.asyncio
async def test_a_capture_that_does_not_know_the_runner_flags_blocks_start_and_pauses_at_once(
    tmp_path: Path, monkeypatch
) -> None:
    """v4 CAPMETA review M1: the runner deployed ahead of the capture script must stop before a
    cycle, with the reason, not lose the recording to "unknown argument" at capture time."""
    old_capture = tmp_path / "capture.sh"
    old_capture.write_text("#!/usr/bin/env bash\n[[ \"$1\" == --capabilities ]] && { echo \"unknown argument: $1\" >&2; exit 1; }\nexit 0\n")
    old_capture.chmod(0o755)
    monkeypatch.setattr("app.incident_close.open_incident_count", lambda **kw: 0)

    queue, runner, _, _, clock = make_queue(tmp_path)
    queue.required_paths = {**queue.required_paths, "capture_script": old_capture}
    queue.preflight_probe = FakePreflightProbe(FakePreflightProbe.CLEAN)
    queue.functional_readiness_enabled = True
    with pytest.raises(RuntimeError, match="capture_contract"):
        await queue.start()
    assert runner.started == []

    # Already running when the capture is swapped: not a transient probe, so no grace.
    queue.functional_readiness_enabled = False
    await queue.start()
    queue.functional_readiness_enabled = True
    _wait_in_clean_window(queue, clock)
    queue._functional_cache = None
    state = await queue.tick()
    assert state.phase == "paused"
    assert "capture_contract" in (state.reason or "")
    assert "deploy testbed-services capture-eval-case.sh first" in (state.reason or "")
    assert runner.started == []


def _oracle_check_dir(
    tmp_path: Path, measured_at: datetime, alert: str | None = None, period_hours: object = 6
) -> Path:
    """What oracle-maintenance.sh check leaves on tb-ops: last.json always, ALERT on a reason."""
    state = tmp_path / "oracle-capacity"
    state.mkdir(exist_ok=True)
    result = {
        "measured_at": measured_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "user_data_bytes": 6602620928,
        "seed_min_balance": 450000,
    }
    if period_hours is not None:
        result["check_period_hours"] = period_hours
    (state / "last.json").write_text(json.dumps(result), encoding="utf-8")
    if alert is None:
        (state / "ALERT").unlink(missing_ok=True)
    else:
        (state / "ALERT").write_text(alert, encoding="utf-8")
    return state


def test_oracle_check_result_gates_readiness(tmp_path: Path) -> None:
    """v4 E5b review 8: the check's ALERT must stop a recording, or nobody reads it.

    A fresh clean result passes. ALERT blocks with its reason lines. No result, a stale
    result (cron stopped) and an unreadable result block too: each is the same blind spot.
    Without a configured directory the check is not part of readiness at all.
    """
    queue, _, _, _, clock = make_queue(tmp_path)
    assert ORACLE_CHECK_NAME not in queue.readiness().checks

    queue.oracle_check_state_dir = tmp_path / "oracle-capacity"
    readiness = queue.readiness()
    assert readiness.checks[ORACLE_CHECK_NAME] is False
    assert "never ran" in readiness.details[ORACLE_CHECK_NAME]

    _oracle_check_dir(tmp_path, clock.value - timedelta(hours=1))
    readiness = queue.readiness()
    assert readiness.checks[ORACLE_CHECK_NAME] is True
    assert readiness.ready

    _oracle_check_dir(
        tmp_path,
        clock.value - timedelta(hours=1),
        alert=(
            "2026-11-10T05:56:29Z seed accounts: 2 below 150000, lowest ACC-1007 73898.69\n"
            "2026-11-10T05:56:29Z banking Oracle user data 11274289153 bytes > 11274289152\n"
        ),
    )
    readiness = queue.readiness()
    assert not readiness.ready
    assert readiness.missing == [ORACLE_CHECK_NAME]
    detail = readiness.details[ORACLE_CHECK_NAME]
    assert detail.startswith("ALERT ")
    assert "lowest ACC-1007 73898.69" in detail and "user data 11274289153" in detail

    # The limit is the check's cron period twice plus an hour (6h -> 13h): one failed run
    # (last.json left as it was) still passes, two in a row block.
    assert ORACLE_CHECK_MAX_AGE == timedelta(hours=13)
    _oracle_check_dir(tmp_path, clock.value - timedelta(hours=12, minutes=59))
    assert queue.readiness().checks[ORACLE_CHECK_NAME] is True
    _oracle_check_dir(tmp_path, clock.value - timedelta(hours=13, minutes=1))
    readiness = queue.readiness()
    assert readiness.checks[ORACLE_CHECK_NAME] is False
    detail = readiness.details[ORACLE_CHECK_NAME]
    assert "stale" in detail and "last successful check at" in detail and "limit 13h" in detail

    # The limit follows the period the check itself recorded (one definition: the script's
    # CHECK_PERIOD_HOURS, which also prints the cron line): 12h -> 25h.
    _oracle_check_dir(tmp_path, clock.value - timedelta(hours=24, minutes=59), period_hours=12)
    assert queue.readiness().checks[ORACLE_CHECK_NAME] is True
    _oracle_check_dir(tmp_path, clock.value - timedelta(hours=25, minutes=1), period_hours=12)
    readiness = queue.readiness()
    assert readiness.checks[ORACLE_CHECK_NAME] is False
    assert "limit 25h" in readiness.details[ORACLE_CHECK_NAME]
    # A result from an older script without the field gets the script's default, 6h -> 13h.
    _oracle_check_dir(tmp_path, clock.value - timedelta(hours=12, minutes=59), period_hours=None)
    assert queue.readiness().checks[ORACLE_CHECK_NAME] is True
    _oracle_check_dir(tmp_path, clock.value - timedelta(hours=13, minutes=1), period_hours=None)
    assert queue.readiness().checks[ORACLE_CHECK_NAME] is False
    # A nonsense period is not trusted.
    for bad in (0, 25, "6", True):
        _oracle_check_dir(tmp_path, clock.value - timedelta(hours=1), period_hours=bad)
        readiness = queue.readiness()
        assert readiness.checks[ORACLE_CHECK_NAME] is False
        assert "check_period_hours" in readiness.details[ORACLE_CHECK_NAME]
    # An explicit limit (ORACLE_CHECK_MAX_AGE_SEC) overrides the recorded period.
    queue.oracle_check_max_age = timedelta(hours=2)
    _oracle_check_dir(tmp_path, clock.value - timedelta(hours=3), period_hours=12)
    assert "limit 2h" in queue.readiness().details[ORACLE_CHECK_NAME]
    queue.oracle_check_max_age = None

    (tmp_path / "oracle-capacity" / "last.json").write_text("{", encoding="utf-8")
    readiness = queue.readiness()
    assert readiness.checks[ORACLE_CHECK_NAME] is False
    assert "unreadable" in readiness.details[ORACLE_CHECK_NAME]


@pytest.mark.asyncio
async def test_oracle_check_alert_refuses_start_and_pauses_between_scenarios(tmp_path: Path) -> None:
    """Not transient: an ALERT needs a person, so the batch stops at once with the reason."""
    queue, runner, _, _, clock = make_queue(tmp_path)
    queue.oracle_check_state_dir = _oracle_check_dir(
        tmp_path, clock.value - timedelta(hours=1), alert="t seed accounts: 1 below 150000\n"
    )
    with pytest.raises(RuntimeError, match=ORACLE_CHECK_NAME):
        await queue.start()
    assert runner.started == []

    _oracle_check_dir(tmp_path, clock.value - timedelta(hours=1))
    await queue.start()
    _wait_in_clean_window(queue, clock)
    _oracle_check_dir(
        tmp_path, clock.value - timedelta(hours=1), alert="t transfer pool: 3 accounts below 500000\n"
    )
    state = await queue.tick()
    assert state.phase == "paused"
    assert ORACLE_CHECK_NAME in (state.reason or "")
    assert "transfer pool: 3 accounts below 500000" in (state.reason or "")

    # resume goes through the same gate: refused while the ALERT stays, allowed once it is gone.
    with pytest.raises(RuntimeError, match=ORACLE_CHECK_NAME):
        await queue.resume()
    assert queue.snapshot().phase == "paused"
    _oracle_check_dir(tmp_path, clock.value - timedelta(hours=1))
    resumed = await queue.resume()
    assert resumed.phase != "paused"


def test_a_purge_failure_blocks_readiness_at_once(tmp_path: Path) -> None:
    """v4 ORAFIX (Q25 answer 2): 11-16 every hourly purge failed with 'container not found' from
    11:23 KST, but readiness went false only at 22:07 when the last good check passed 13 hours.
    oracle-maintenance.sh purge now leaves PURGE_FAILED the moment it cannot reach the container:
    readiness is false at once, with a fresh check result, and true again once the file is gone."""
    queue, _, _, _, clock = make_queue(tmp_path)
    queue.oracle_check_state_dir = _oracle_check_dir(tmp_path, clock.value - timedelta(minutes=30))
    assert queue.readiness().checks[ORACLE_CHECK_NAME] is True
    marker = tmp_path / "oracle-capacity" / "PURGE_FAILED"
    marker.write_text(
        "2026-11-16T02:23:01Z purge failed: in testbed-oracle-0: error: Internal error occurred: "
        'unable to upgrade connection: container not found ("oracle")\n',
        encoding="utf-8",
    )
    readiness = queue.readiness()
    assert not readiness.ready and readiness.missing == [ORACLE_CHECK_NAME]
    detail = readiness.details[ORACLE_CHECK_NAME]
    assert detail.startswith("PURGE_FAILED 2026-11-16T02:23:01Z purge failed: in testbed-oracle-0")
    assert 'container not found ("oracle")' in detail and "next purge" in detail
    # It is read before ALERT and before staleness: the newest failure is the one shown.
    _oracle_check_dir(tmp_path, clock.value - timedelta(hours=20), alert="t seed accounts: 1 below 150000\n")
    assert queue.readiness().details[ORACLE_CHECK_NAME].startswith("PURGE_FAILED ")
    marker.write_text("", encoding="utf-8")
    assert "empty PURGE_FAILED file" in queue.readiness().details[ORACLE_CHECK_NAME]
    marker.unlink()
    _oracle_check_dir(tmp_path, clock.value - timedelta(minutes=30))
    assert queue.readiness().checks[ORACLE_CHECK_NAME] is True


@pytest.mark.asyncio
async def test_a_domain_pod_not_ready_holds_then_pauses_before_the_injection_only(tmp_path: Path) -> None:
    """v4 ORAFIX (Q25 answer 2, review rv-orafix H1): every testbed app pod Ready is judged right
    before an injection starts, not in readiness() (read while an injection may hold a pod down on
    purpose). A not-Ready answer first holds the injection and is asked again at most every 30s; only
    when it stays not Ready for the grace (3 minutes) does the queue pause."""
    from app.live_queue import DOMAIN_PODS_GRACE, DOMAIN_PODS_RECHECK

    assert (DOMAIN_PODS_GRACE, DOMAIN_PODS_RECHECK) == (timedelta(minutes=3), timedelta(seconds=30))
    queue, runner, _, _, clock = make_queue(tmp_path)
    answers = ["1 of 7 app pods not Ready: rca-testbed-banking/testbed-oracle-0(CrashLoopBackOff)"]
    calls = []

    def probe():
        calls.append(clock.value)
        return answers[0]

    queue.domain_pods_probe = probe
    readiness = queue.readiness()
    assert DOMAIN_PODS_CHECK_NAME not in readiness.checks and calls == []
    await queue.start()
    state = await queue.tick()
    assert state.phase != "paused" and runner.started == [] and len(calls) == 1
    state = await queue.tick()  # within 30s: no new look, still held
    assert state.phase != "paused" and len(calls) == 1
    clock.value += timedelta(seconds=31)
    state = await queue.tick()
    assert state.phase != "paused" and len(calls) == 2 and runner.started == []
    # The queue ticks every few seconds, so a held gate is asked again every 31s here until 3 minutes.
    while state.phase != "paused" and len(calls) < 10:
        clock.value += timedelta(seconds=31)
        state = await queue.tick()
    assert state.phase == "paused" and runner.started == [] and len(calls) == 7
    assert state.reason == (
        f"operational readiness failed: {DOMAIN_PODS_CHECK_NAME} ({DOMAIN_PODS_CHECK_NAME}: "
        "1 of 7 app pods not Ready: rca-testbed-banking/testbed-oracle-0(CrashLoopBackOff); "
        "not Ready for 186s, limit 180s)"
    )
    answers[0] = None
    await queue.resume()
    state = await queue.tick()
    assert runner.started == [("F01-R", "run")]


@pytest.mark.asyncio
async def test_a_brief_not_ready_or_a_failed_list_does_not_pause(tmp_path: Path) -> None:
    """One liveness restart, one failed readiness period or one API timeout passes (review H1)."""
    queue, runner, _, _, clock = make_queue(tmp_path)
    answers = iter([RuntimeError("Unable to connect to the server: net/http: TLS handshake timeout"),
                    "1 of 31 app pods not Ready: rca-testbed-commerce/testbed-cart-1(Running)", None])

    def probe():
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    queue.domain_pods_probe = probe
    await queue.start()
    state = await queue.tick()
    assert state.phase != "paused" and runner.started == []
    assert queue._domain_pods_detail.startswith("probe failed: RuntimeError: Unable to connect")
    for _ in range(2):
        clock.value += timedelta(seconds=40)
        state = await queue.tick()
    assert state.phase != "paused"
    assert runner.started == [("F01-R", "run")]
    # The hold is forgotten once the pods were Ready: a later not-Ready starts a new grace.
    assert queue._domain_pods_since is None
    queue.domain_pods_probe = None
    assert queue._domain_pods_gate() == ("go", None)


def test_a_hold_left_another_way_does_not_shorten_the_next_grace(tmp_path: Path) -> None:
    """Review rv-orafix R2-M1: a hold whose last look is old was left another way (deferral, cycle
    restart, pause and resume, operator stop); a later single not-Ready starts a new grace instead of
    pausing at once with the old start."""
    from app.live_queue import DOMAIN_PODS_HOLD_GAP

    assert DOMAIN_PODS_HOLD_GAP == timedelta(seconds=90)
    queue, _, _, _, clock = make_queue(tmp_path)
    queue.domain_pods_probe = lambda: "1 of 31 app pods not Ready: rca-testbed-commerce/testbed-cart-1(Running)"
    assert queue._domain_pods_gate()[0] == "hold"
    started = queue._domain_pods_since
    clock.value += timedelta(hours=2)
    verdict, detail = queue._domain_pods_gate()
    assert verdict == "hold" and "testbed-cart-1" in detail
    assert queue._domain_pods_since == clock.value != started
    # An unbroken hold (looked again within the gap) still counts from its first look.
    for _ in range(6):
        clock.value += timedelta(seconds=31)
        verdict, _ = queue._domain_pods_gate()
    assert verdict == "pause" and queue._domain_pods_since is None
    # A gap just over the limit restarts; one at the limit does not.
    assert queue._domain_pods_gate()[0] == "hold"
    clock.value += timedelta(seconds=90)
    assert queue._domain_pods_gate()[0] == "hold"
    first = queue._domain_pods_since
    assert first is not None and first == clock.value - timedelta(seconds=90)
    clock.value += timedelta(seconds=91)
    queue._domain_pods_gate()
    assert queue._domain_pods_since == clock.value


@pytest.mark.asyncio
async def test_a_hold_before_a_pause_and_resume_gets_a_fresh_grace(tmp_path: Path) -> None:
    """The queue path the review reproduced: held on a not-Ready pod, paused on another check (the
    Oracle ALERT), resumed two hours later, then one not-Ready answer: held again, not paused."""
    queue, runner, _, _, clock = make_queue(tmp_path)
    answers = ["1 of 31 app pods not Ready: rca-testbed-commerce/testbed-cart-1(Running)"]
    queue.domain_pods_probe = lambda: answers[0]
    queue.oracle_check_state_dir = _oracle_check_dir(tmp_path, clock.value - timedelta(minutes=30))
    await queue.start()
    state = await queue.tick()
    assert state.phase != "paused" and runner.started == []
    _wait_in_clean_window(queue, clock)
    _oracle_check_dir(tmp_path, clock.value - timedelta(minutes=30), alert="t seed accounts: 1 below 150000\n")
    state = await queue.tick()
    assert state.phase == "paused" and ORACLE_CHECK_NAME in (state.reason or "")
    clock.value += timedelta(hours=2)
    _oracle_check_dir(tmp_path, clock.value - timedelta(minutes=30))
    await queue.resume()
    for _ in range(3):
        state = await queue.tick()
        if state.phase == "paused":
            break
    assert state.phase != "paused", state.reason
    assert runner.started == []
    answers[0] = None
    clock.value += timedelta(seconds=31)
    for _ in range(3):
        state = await queue.tick()
    assert runner.started == [("F01-R", "run")]


def test_compose_turns_the_domain_pods_check_on() -> None:
    compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text(encoding="utf-8")
    assert "- DOMAIN_PODS_CHECK=${DOMAIN_PODS_CHECK:-1}" in compose


def _archive_facts(
    tmp_path: Path, at: datetime, *, rows: int = 2_080_000, last_ok: datetime | None | str = "default"
) -> Path:
    """What civil-petition/deploy/tools/db-facts.sh prints, left as last.json by the periodic db01 read."""
    state = tmp_path / "petition-archive"
    state.mkdir(exist_ok=True)
    if last_ok == "default":
        last_ok = at - timedelta(hours=12)
    doc = {
        "host": "db01",
        "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "db": {
            "rows": {"petition": rows, "petition_history": rows * 3},
            "archive_runs": [],
            "last_archive_ok": None if last_ok is None else last_ok.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        # Computed at install from the load settings (cap 2,000,000 + 121,824 x 2.5).
        "rows_limit": {"limit": 2_304_560, "keep_rows": 2_000_000, "design_daily_writes": 121_824, "days": 2.5},
        "archive_schedule": {"timer": "02:20", "window_end": "05:00", "zone": "Asia/Seoul", "timer_zone": "Asia/Seoul"},
    }
    (state / "last.json").write_text(json.dumps(doc), encoding="utf-8")
    return state


def test_archive_state_gates_readiness(tmp_path: Path) -> None:
    """Q22 addendum 2: block before injecting when the civil-petition archive stopped keeping up.

    Two nightly windows closed without an ok (counted in windows, see the next test) or rows over the limit the db01 line
    carries (cap + design daily writes x 2.5, Q24) blocks. Facts that are missing, stale or unreadable block too.
    Without a configured directory the check is not part of readiness.
    """
    queue, _, _, _, clock = make_queue(tmp_path)
    assert ARCHIVE_STATE_CHECK_NAME not in queue.readiness().checks

    queue.archive_state_dir = tmp_path / "petition-archive"
    readiness = queue.readiness()
    assert readiness.checks[ARCHIVE_STATE_CHECK_NAME] is False
    assert "never ran" in readiness.details[ARCHIVE_STATE_CHECK_NAME]

    now = clock.value
    _archive_facts(tmp_path, now - timedelta(minutes=5))
    readiness = queue.readiness()
    assert readiness.checks[ARCHIVE_STATE_CHECK_NAME] is True
    assert readiness.ready

    # Two missed windows block (now = 17:00 KST; last ok three days earlier: two full windows closed since).
    _archive_facts(tmp_path, now, last_ok=now - timedelta(days=2, hours=13))
    readiness = queue.readiness()
    assert readiness.missing == [ARCHIVE_STATE_CHECK_NAME]
    assert "2 nightly windows (02:20-05:00 Asia/Seoul) closed since without an ok" in readiness.details[ARCHIVE_STATE_CHECK_NAME]
    _archive_facts(tmp_path, now, last_ok=None)
    assert "no archive batch has ever finished ok" in queue.readiness().details[ARCHIVE_STATE_CHECK_NAME]

    # Rows over the limit: 2,304,560 passes, 2,304,561 blocks; both reasons are listed. One failed night
    # (cap + two days of writes, 2,243,648) still passes.
    _archive_facts(tmp_path, now, rows=2_000_000 + 2 * 121_824)
    assert queue.readiness().checks[ARCHIVE_STATE_CHECK_NAME] is True
    _archive_facts(tmp_path, now, rows=2_304_560)
    assert queue.readiness().checks[ARCHIVE_STATE_CHECK_NAME] is True
    _archive_facts(tmp_path, now, rows=2_304_561)
    assert (
        "petition rows 2304561 > 2304560 (cap 2000000 + design daily writes 121824 x 2.5)"
        in queue.readiness().details[ARCHIVE_STATE_CHECK_NAME]
    )
    _archive_facts(tmp_path, now, rows=2_304_561, last_ok=now - timedelta(days=2, hours=13))
    detail = queue.readiness().details[ARCHIVE_STATE_CHECK_NAME]
    assert "2 nightly windows" in detail and "2304561" in detail
    # The limit is the one the line carries (computed from the load settings), not a runner constant.
    state = tmp_path / "petition-archive" / "last.json"
    doc = json.loads(state.read_text())
    doc["rows_limit"] = {"limit": 1_100_000, "keep_rows": 1_000_000, "design_daily_writes": 40_000, "days": 2.5}
    doc["db"]["rows"]["petition"] = 1_100_001
    state.write_text(json.dumps(doc), encoding="utf-8")
    assert "> 1100000" in queue.readiness().details[ARCHIVE_STATE_CHECK_NAME]
    # Review L11: a limit that does not match its own parts (hand edit, broken render) is not trusted.
    for broken in ({"limit": 9_000_000}, {"keep_rows": 1_200_000}, {"days": 0}, {"design_daily_writes": True}):
        doc["rows_limit"] = {"limit": 1_100_000, "keep_rows": 1_000_000, "design_daily_writes": 40_000, "days": 2.5, **broken}
        state.write_text(json.dumps(doc), encoding="utf-8")
        assert "unreadable" in queue.readiness().details[ARCHIVE_STATE_CHECK_NAME], broken
    # A line without the limit or the schedule cannot be judged and blocks.
    doc["rows_limit"] = {"limit": 1_100_000, "keep_rows": 1_000_000, "design_daily_writes": 40_000, "days": 2.5}
    for key in ("rows_limit", "archive_schedule"):
        broken_doc = {k: v for k, v in doc.items() if k != key}
        state.write_text(json.dumps(broken_doc), encoding="utf-8")
        assert "unreadable" in queue.readiness().details[ARCHIVE_STATE_CHECK_NAME], key

    # Facts older than the limit (default 75 min = two missed 30-minute timer runs) say nothing about now.
    _archive_facts(tmp_path, now - timedelta(minutes=74))
    assert queue.readiness().checks[ARCHIVE_STATE_CHECK_NAME] is True
    _archive_facts(tmp_path, now - timedelta(minutes=76))
    assert "stale" in queue.readiness().details[ARCHIVE_STATE_CHECK_NAME]

    # Review L15: a timer zone that differs from the app window zone cannot be counted and blocks with why.
    state = tmp_path / "petition-archive" / "last.json"
    _archive_facts(tmp_path, now)
    doc = json.loads(state.read_text())
    doc["archive_schedule"]["timer_zone"] = "UTC"
    state.write_text(json.dumps(doc), encoding="utf-8")
    readiness = queue.readiness()
    assert readiness.checks[ARCHIVE_STATE_CHECK_NAME] is False
    assert "archive timer zone 'UTC' != app window zone 'Asia/Seoul'" in readiness.details[ARCHIVE_STATE_CHECK_NAME]
    del doc["archive_schedule"]["timer_zone"]
    state.write_text(json.dumps(doc), encoding="utf-8")
    assert "archive timer zone None" in queue.readiness().details[ARCHIVE_STATE_CHECK_NAME]

    (tmp_path / "petition-archive" / "last.json").write_text('{"at": "2026-07-16T08:00:00Z"}', encoding="utf-8")
    readiness = queue.readiness()
    assert readiness.checks[ARCHIVE_STATE_CHECK_NAME] is False
    assert "unreadable" in readiness.details[ARCHIVE_STATE_CHECK_NAME]


def test_archive_state_counts_missed_nightly_windows_not_hours(tmp_path: Path) -> None:
    """Review rv-t2c M3, command-session decision: one failed night is tolerated all through the next
    night's catch-up (a plain 48h age blocked 02:22-02:40 and paused the queue at dawn); two failed nights
    in a row block from the first line read after the second window's end; a line that stops coming
    blocks within the staleness limit."""
    kst = ZoneInfo("Asia/Seoul")
    queue, _, _, _, clock = make_queue(tmp_path)
    queue.archive_state_dir = tmp_path / "petition-archive"
    first_ok = datetime(2026, 11, 16, 2, 22, 30, tzinfo=kst)        # day 0 ok
    catch_up_ok = datetime(2026, 11, 18, 2, 24, 10, tzinfo=kst)     # day 1 failed, day 2 catches up

    # One failure: every 5 minutes 02:00-06:00 on day 2, the line read at :05 and :35, the runner 4 min later.
    t = datetime(2026, 11, 18, 2, 0, tzinfo=kst)
    while t <= datetime(2026, 11, 18, 6, 0, tzinfo=kst):
        line_at = t.replace(minute=35 if t.minute >= 35 else 5) if t.minute >= 5 else t - timedelta(minutes=25)
        last_ok = first_ok if line_at < catch_up_ok else catch_up_ok
        clock.value = (t + timedelta(minutes=4)).astimezone(timezone.utc)
        _archive_facts(tmp_path, line_at.astimezone(timezone.utc), last_ok=last_ok.astimezone(timezone.utc))
        readiness = queue.readiness()
        assert readiness.checks[ARCHIVE_STATE_CHECK_NAME] is True, (t, readiness.details)
        t += timedelta(minutes=5)

    # Two failures in a row (day 1 and day 2): passes on lines read before the day-2 window end, blocks after.
    for line_at, passes in (
        (datetime(2026, 11, 18, 4, 35, tzinfo=kst), True),
        (datetime(2026, 11, 18, 4, 59, 59, tzinfo=kst), True),
        (datetime(2026, 11, 18, 5, 0, tzinfo=kst), False),
        (datetime(2026, 11, 18, 5, 5, tzinfo=kst), False),
    ):
        clock.value = (line_at + timedelta(minutes=4)).astimezone(timezone.utc)
        _archive_facts(tmp_path, line_at.astimezone(timezone.utc), last_ok=first_ok.astimezone(timezone.utc))
        readiness = queue.readiness()
        assert readiness.checks[ARCHIVE_STATE_CHECK_NAME] is passes, (line_at, readiness.details)
        if not passes:
            assert "2 nightly windows" in readiness.details[ARCHIVE_STATE_CHECK_NAME]

    # Staleness (review L14): one missed timer run plus the fetch delay (up to about 65 min) still passes,
    # two missed runs block (limit 75 min). The 04:35 line is the last before the day-2 window end.
    line_at = datetime(2026, 11, 18, 4, 35, tzinfo=kst)
    _archive_facts(tmp_path, line_at.astimezone(timezone.utc), last_ok=first_ok.astimezone(timezone.utc))
    for minutes, passes in ((35, True), (65, True), (74, True), (76, False)):
        clock.value = (line_at + timedelta(minutes=minutes)).astimezone(timezone.utc)
        readiness = queue.readiness()
        assert readiness.checks[ARCHIVE_STATE_CHECK_NAME] is passes, (minutes, readiness.details)
    assert "stale" in readiness.details[ARCHIVE_STATE_CHECK_NAME] and "limit 75m" in readiness.details[ARCHIVE_STATE_CHECK_NAME]


@pytest.mark.asyncio
async def test_archive_state_refuses_start_pauses_between_scenarios_and_is_recorded(tmp_path: Path) -> None:
    """Not transient: a stalled archive needs a person. The facts an injection started on are
    left in the run folder (archive_state.json) for the verify skill."""
    queue, runner, _, _, clock = make_queue(tmp_path)
    queue.archive_state_dir = _archive_facts(tmp_path, clock.value, rows=2_400_000)
    with pytest.raises(RuntimeError, match=ARCHIVE_STATE_CHECK_NAME):
        await queue.start()
    assert runner.started == []

    _archive_facts(tmp_path, clock.value)
    await queue.start()
    state = await queue.tick()
    assert runner.started == [("F01-R", "run")]
    run_id = state.current_run_id
    recorded = json.loads((runner.artifact_store.root / run_id / "archive_state.json").read_text())
    assert recorded["passes"] is True
    assert recorded["facts"]["db"]["rows"]["petition"] == 2_080_000
    assert recorded["rows_limit"] == {"limit": 2_304_560, "keep_rows": 2_000_000, "design_daily_writes": 121_824, "days": 2.5}
    assert recorded["missed_windows_limit"] == 2
    assert recorded["archive_schedule"] == {"timer": "02:20", "window_end": "05:00", "zone": "Asia/Seoul", "timer_zone": "Asia/Seoul"}

    _wait_in_clean_window(queue, clock)
    started = len(runner.started)
    _archive_facts(tmp_path, clock.value, last_ok=clock.value - timedelta(days=2, hours=13))
    state = await queue.tick()
    assert state.phase == "paused"
    assert ARCHIVE_STATE_CHECK_NAME in (state.reason or "") and "2 nightly windows" in (state.reason or "")
    assert len(runner.started) == started

    with pytest.raises(RuntimeError, match=ARCHIVE_STATE_CHECK_NAME):
        await queue.resume()
    _archive_facts(tmp_path, clock.value)
    assert (await queue.resume()).phase != "paused"


def test_compose_passes_the_archive_state_settings_and_leaves_the_check_off() -> None:
    compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text(encoding="utf-8")
    assert "- PETITION_ARCHIVE_STATE_DIR=${PETITION_ARCHIVE_STATE_DIR:-}" in compose
    # The rows limit comes with the db01 line (computed from the load settings), not from runner settings.
    assert "PETITION_ARCHIVE_KEEP_ROWS" not in compose


def test_compose_mounts_the_oracle_check_directory_read_only() -> None:
    compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text(encoding="utf-8")
    assert "- ${ORACLE_CHECK_HOST_PATH:-/var/lib/lucida/oracle-capacity}:/var/lib/lucida/oracle-capacity:ro" in compose
    assert "- ORACLE_CHECK_STATE_DIR=${ORACLE_CHECK_STATE_DIR:-/var/lib/lucida/oracle-capacity}" in compose


@pytest.mark.asyncio
async def test_smoke_pass_does_not_gate_the_batch_on_the_unused_capture_chain(
    tmp_path: Path, monkeypatch
) -> None:
    """A smoke pass exports nothing, so a broken 119 must not stop it.

    Both 2026-08-04 pauses were the capture chain on a busy AP server, during a
    pass that never captures. The dataset pass still proves the chain — losing a
    case to an unprovable export is the loss this check exists to prevent — but
    it gets the same grace as the probe rather than an instant stop.
    """
    script = tmp_path / "capture.sh"
    script.write_text("#!/usr/bin/env bash\n[[ \"$1\" == --capabilities ]] && { echo \"capabilities: scenario-doc-file source-commits-json capture-script-meta\"; exit 0; }\necho '[ERROR] clickhouse' >&2\nexit 1\n")
    script.chmod(0o755)
    monkeypatch.setattr("app.incident_close.open_incident_count", lambda **kw: 0)

    queue, runner, _, _, clock = make_queue(tmp_path)
    queue.required_paths = {**queue.required_paths, "capture_script": script}
    queue.preflight_probe = FakePreflightProbe(FakePreflightProbe.CLEAN)
    await queue.start()
    queue.functional_readiness_enabled = True
    _wait_in_clean_window(queue, clock)

    monkeypatch.setenv("SCENARIO_PASS", "smoke")
    queue._functional_cache = None
    readiness = queue.readiness()
    assert "capture_self_check" not in readiness.checks
    assert readiness.ready, "a smoke pass must not depend on the capture chain"

    monkeypatch.setenv("SCENARIO_PASS", "dataset")
    queue._functional_cache = None
    readiness = queue.readiness()
    assert readiness.checks["capture_self_check"] is False

    state = await queue.tick()
    assert state.phase == "waiting_clean_window", "dataset waits out the grace"
    clock.value += READINESS_PROBE_GRACE + timedelta(seconds=5)
    queue._functional_cache = None
    state = await queue.tick()
    assert state.phase == "paused"
    assert "capture_self_check" in (state.reason or "")
    assert runner.started == []


def test_normal_segment_marker_is_fail_open_and_points_at_todays_kst_segment(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from datetime import datetime, timezone

    from app.live_queue import LiveScenarioQueue as LiveQueue

    runs = tmp_path / "runs"
    segment_root = tmp_path / "normal-segments"
    # 2026-07-20T16:30Z = 2026-07-21 01:30 KST — the KST date must win.
    clock = SimpleNamespace(now=lambda: datetime(2026, 7, 20, 16, 30, tzinfo=timezone.utc))
    stub = SimpleNamespace(
        clock=clock,
        runner=SimpleNamespace(artifact_store=SimpleNamespace(root=runs)),
    )

    monkeypatch.delenv("NORMAL_SEGMENT_ROOT", raising=False)
    LiveQueue._write_normal_segment_marker(stub, "run-1")
    assert not (runs / "run-1" / "normal-segment.path").exists()

    monkeypatch.setenv("NORMAL_SEGMENT_ROOT", str(segment_root))
    LiveQueue._write_normal_segment_marker(stub, "run-1")
    assert not (runs / "run-1" / "normal-segment.path").exists()

    segment = segment_root / "commerce" / "2026-07-21"
    segment.mkdir(parents=True)
    (segment / "meta.json").write_text("{}", encoding="utf-8")
    LiveQueue._write_normal_segment_marker(stub, "run-1")
    marker = runs / "run-1" / "normal-segment.path"
    assert marker.read_text(encoding="utf-8").strip() == str(segment)


def test_protection_window_defers_injection_starts_around_kst_midnight():
    from datetime import datetime, timezone

    from app.live_queue import LiveScenarioQueue

    def at_kst(hour, minute):
        # KST = UTC+9
        return datetime(2026, 7, 20, (hour - 9) % 24, minute, tzinfo=timezone.utc)

    assert LiveScenarioQueue._protection_window_reason(at_kst(12, 0)) is None
    assert LiveScenarioQueue._protection_window_reason(at_kst(23, 19)) is None
    for hour, minute in ((23, 20), (0, 0), (1, 30), (2, 29)):
        assert LiveScenarioQueue._protection_window_reason(at_kst(hour, minute)) is not None
    assert LiveScenarioQueue._protection_window_reason(at_kst(2, 30)) is None


def test_protection_window_is_released_only_by_the_smoke_pass(monkeypatch):
    """A pass that discards its captures should not lose ~3h of every 24h.

    Only the declared smoke pass releases the window — unset or any other value
    keeps the dataset-grade default, since forgetting it would silently poison
    the shared daily normal prefix that every case of that day is built on.
    """
    from datetime import datetime, timezone

    from app.live_queue import LiveScenarioQueue

    midnight_kst = datetime(2026, 7, 20, 15, 0, tzinfo=timezone.utc)  # 00:00 KST

    monkeypatch.delenv("SCENARIO_PASS", raising=False)
    assert LiveScenarioQueue._protection_window_reason(midnight_kst) is not None

    monkeypatch.setenv("SCENARIO_PASS", "dataset")
    assert LiveScenarioQueue._protection_window_reason(midnight_kst) is not None

    monkeypatch.setenv("SCENARIO_PASS", "smoke")
    assert LiveScenarioQueue._protection_window_reason(midnight_kst) is None


async def test_clean_window_tick_runs_off_the_event_loop():
    """#31 (2026-08-03 batch): the clean-window tick re-probes readiness, which
    shells out to the capture self-check for up to 120s. Served inline it froze
    the event loop and starved the coordinator heartbeat until the active run's
    30s lease expired — every later operation then died on fencing rejection.
    The sync ticks must run on a thread so heartbeats keep flowing."""
    import asyncio
    import time
    from types import SimpleNamespace

    from app.live_queue import LiveScenarioQueue

    state = SimpleNamespace(phase="waiting_clean_window", cycle_mode=False)
    stub = SimpleNamespace(
        _lock=asyncio.Lock(),
        snapshot=lambda: state,
        runner=SimpleNamespace(
            coordinator=SimpleNamespace(
                snapshot=lambda: SimpleNamespace(dirty_run=None)
            )
        ),
        _append_promoted_if_available=lambda s: s,
        _tick_clean_window=lambda s: (time.sleep(0.5), s)[1],
    )

    beats = 0

    async def heartbeat() -> None:
        nonlocal beats
        while True:
            await asyncio.sleep(0.02)
            beats += 1

    task = asyncio.create_task(heartbeat())
    try:
        result = await LiveScenarioQueue.tick(stub)
    finally:
        task.cancel()
    assert result is state
    # Inline execution starves the loop completely (0 beats in 0.5s); off-loop
    # execution keeps it beating. Require a comfortable margin, not the ideal.
    assert beats >= 5


def test_functional_readiness_is_opt_in_and_gates_on_capture_self_check(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from app.live_queue import LiveScenarioQueue

    script = tmp_path / "capture.sh"
    script.write_text("#!/usr/bin/env bash\nexit 1\n")
    script.chmod(0o755)
    stub = SimpleNamespace(
        functional_readiness_enabled=False,
        clock=SimpleNamespace(now=lambda: datetime.now(timezone.utc)),
        required_paths={"capture_script": script},
        preflight_probe=None,
        _functional_cache=None,
    )
    assert LiveScenarioQueue._functional_readiness(stub) == {}

    stub.functional_readiness_enabled = True
    monkeypatch.setenv("LUCIDA_LOGIN_USER", "")
    checks = LiveScenarioQueue._functional_readiness(stub)
    assert checks["capture_self_check"] is False
    assert checks["lucida_incident_api"] is False
    assert checks["capture_contract"] is False

    script.write_text("#!/usr/bin/env bash\nexit 0\n")
    stub._functional_cache = None
    monkeypatch.setattr(
        "app.incident_close.open_incident_count", lambda **kwargs: 0
    )
    checks = LiveScenarioQueue._functional_readiness(stub)
    assert checks["capture_self_check"] is True
    assert checks["lucida_incident_api"] is True
    # v4 CAPMETA review M1: a capture that passes its self-check but does not know the
    # flags this runner passes (an old version, or the host file replaced under a
    # single-file bind mount) is a separate, named failure.
    assert checks["capture_contract"] is False
    assert "scenario-doc-file, source-commits-json" in stub._functional_details["capture_contract"]
    assert "recreate the runner container" in stub._functional_details["capture_contract"]

    script.write_text(
        "#!/usr/bin/env bash\n"
        '[[ "$1" == --capabilities ]] && { echo "capabilities: scenario-doc-file"; exit 0; }\nexit 0\n'
    )
    stub._functional_cache = None
    checks = LiveScenarioQueue._functional_readiness(stub)
    assert checks["capture_contract"] is False
    assert "lacks source-commits-json" in stub._functional_details["capture_contract"]

    script.write_text(
        "#!/usr/bin/env bash\n"
        '[[ "$1" == --capabilities ]] && { echo "capabilities: scenario-doc-file source-commits-json x"; exit 0; }\n'
        "exit 0\n"
    )
    stub._functional_cache = None
    checks = LiveScenarioQueue._functional_readiness(stub)
    assert checks["capture_contract"] is True
    assert "capture_contract" not in stub._functional_details


def test_lingering_incident_sweep_only_fires_when_incidents_are_the_sole_blocker(monkeypatch):
    from types import SimpleNamespace

    from app.live_queue import LiveScenarioQueue

    def check(name, status):
        return SimpleNamespace(name=name, status=status)

    closed = []
    monkeypatch.setattr(
        "app.incident_close.close_open_incidents", lambda **kwargs: closed.append(True) or []
    )
    stub = SimpleNamespace()

    only_incidents = SimpleNamespace(checks=[check("open_incidents", "fail"), check("user_5xx_rate", "pass")])
    assert LiveScenarioQueue._sweep_lingering_incidents(stub, only_incidents) is True
    assert closed == [True]

    mixed = SimpleNamespace(checks=[check("open_incidents", "fail"), check("user_5xx_rate", "fail")])
    assert LiveScenarioQueue._sweep_lingering_incidents(stub, mixed) is False
    all_pass = SimpleNamespace(checks=[check("open_incidents", "pass")])
    assert LiveScenarioQueue._sweep_lingering_incidents(stub, all_pass) is False
    assert closed == [True]


def test_a_refused_sweep_is_kept_for_the_reason_not_swallowed(monkeypatch, caplog):
    """11-13: every close was refused (403 forbidden origin) and the sweep
    returned False with nothing logged, so the restart said only "(dirty)"."""
    from types import SimpleNamespace

    from app.incident_close import IncidentCloseError
    from app.live_queue import LiveScenarioQueue
    from app.preflight import PreflightCheckInput, evaluate_preflight

    def refuse(**kwargs):
        raise IncidentCloseError(
            closed=[],
            failed=[{"id": "inc-1", "status": "active", "title": "t",
                     "error": 'HTTP 403 {"message":"forbidden origin"}'}],
        )

    monkeypatch.setattr("app.incident_close.close_open_incidents", refuse)
    stub = SimpleNamespace(last_sweep_error="stale")
    now = datetime(2026, 11, 13, 6, 39, tzinfo=timezone.utc)
    verdict = evaluate_preflight(
        window=("a", "b"),
        checks=[
            PreflightCheckInput(name="open_incidents", value=102.0, threshold=0.0, op="eq"),
            PreflightCheckInput(name="active_alarms", value=0.0, threshold=0.0, op="eq"),
        ],
        now=now,
    )
    with caplog.at_level("WARNING", logger="scenario_runner.live_queue"):
        assert LiveScenarioQueue._sweep_lingering_incidents(stub, verdict) is False
    assert "forbidden origin" in stub.last_sweep_error
    assert "lingering incident sweep failed" in caplog.text

    detail = LiveScenarioQueue._not_clean_detail(stub, verdict)
    assert detail.startswith("open_incidents=102 (fail, eq 0)")
    assert "active_alarms" not in detail
    assert "incident sweep failed: IncidentCloseError" in detail
    assert "forbidden origin" in detail

    # A sweep that was not attempted clears the previous refusal.
    mixed = evaluate_preflight(
        window=("a", "b"),
        checks=[
            PreflightCheckInput(name="open_incidents", value=1.0, threshold=0.0, op="eq"),
            PreflightCheckInput(name="active_alarms", value=2.0, threshold=0.0, op="eq"),
        ],
        now=now,
    )
    assert LiveScenarioQueue._sweep_lingering_incidents(stub, mixed) is False
    assert stub.last_sweep_error is None
    assert LiveScenarioQueue._not_clean_detail(stub, mixed) == (
        "open_incidents=1 (fail, eq 0); active_alarms=2 (fail, eq 0)"
    )


class FakeRestoreRunner:
    """Capture golden-restore invocations and record injection ordering."""

    def __init__(self, runner: Runner, *, fail: bool = False) -> None:
        self._runner = runner
        self.fail = fail
        self.calls: list[list[str]] = []
        self.started_len_at_call: list[int] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        self.started_len_at_call.append(len(self._runner.started))
        if self.fail:
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    @property
    def remotes(self) -> list[str]:
        return [cmd[-1] for cmd in self.calls]


def _enable_golden(queue: LiveScenarioQueue, runner: Runner, *, fail: bool = False):
    restore = FakeRestoreRunner(runner, fail=fail)
    ssh_key = queue.state_path.parent / "ssh_key"
    ssh_key.write_text("key", encoding="utf-8")
    queue.golden_reset_enabled = True
    queue.restore_retry_delay_sec = 0
    queue._restore_runner = restore  # type: ignore[assignment]
    queue.required_paths["ssh_key"] = ssh_key
    return restore


async def _drive_to_capture_complete(queue, runner, scheduler, clock):
    await queue.start()
    state = await queue.tick()
    run_id = state.current_run_id
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
    capture.parent.mkdir(parents=True, exist_ok=True)
    capture.write_text("{}\n", encoding="utf-8")
    runner.current = None
    return await queue.tick()


@pytest.mark.asyncio
async def test_golden_restore_runs_before_injection_when_enabled(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MODEL_SNAPSHOT_SSH_TARGET", "ydkim@192.168.230.119")
    queue, runner, _, _, _ = make_queue(tmp_path)
    restore = _enable_golden(queue, runner)
    await queue.start()
    await queue.tick()

    assert runner.started == [("F01-R", "run")]
    assert restore.calls, "golden restore was not invoked"
    remote = restore.remotes[0]
    assert "--golden-root" in remote
    assert "--for-time" in remote
    assert "now" in remote
    # restore ran before runner.start appended the injection
    assert restore.started_len_at_call[0] == 0


@pytest.mark.asyncio
async def test_golden_restore_noop_when_disabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MODEL_SNAPSHOT_SSH_TARGET", "ydkim@192.168.230.119")
    queue, runner, _, _, _ = make_queue(tmp_path)
    restore = FakeRestoreRunner(runner)
    queue._restore_runner = restore  # type: ignore[assignment]
    # golden_reset_enabled stays default False
    await queue.start()
    await queue.tick()

    assert runner.started == [("F01-R", "run")]
    assert restore.calls == []


@pytest.mark.asyncio
async def test_thaw_runs_at_capture_complete_when_enabled(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MODEL_SNAPSHOT_SSH_TARGET", "ydkim@192.168.230.119")
    queue, runner, _, scheduler, clock = make_queue(tmp_path)
    restore = _enable_golden(queue, runner)
    state = await _drive_to_capture_complete(queue, runner, scheduler, clock)

    assert state.phase == "waiting_clean_window"
    assert any("--thaw" in remote for remote in restore.remotes)


@pytest.mark.asyncio
async def test_restore_failure_pauses_queue(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MODEL_SNAPSHOT_SSH_TARGET", "ydkim@192.168.230.119")
    queue, runner, _, _, _ = make_queue(tmp_path)
    _enable_golden(queue, runner, fail=True)
    await queue.start()
    state = await queue.tick()

    assert state.phase == "paused"
    assert "golden restore failed" in state.reason
    assert runner.started == []


# --- VM testbed (civil-petition): domain preflight and drift gate (spec-testbed-vm-3tier §6-4, §6-6) -----------

from tests.test_preflight import VM_CLEAN


@pytest.mark.asyncio
async def test_a_scenario_whose_domain_cannot_be_read_is_not_injected(tmp_path: Path) -> None:
    """Review rv-t4r3 M2: the domain picks the probe, the signal set and the drift gate. A registry that cannot be
    read, or a scenario without an entry, stops the injection with why instead of falling to the R6 signals."""
    from app.live_queue import SCENARIO_METADATA_CHECK_NAME

    queue, runner, _, _, clock = make_queue(tmp_path)
    _make_vm_domain(runner, "F01-R")
    default = FakePreflightProbe(FakePreflightProbe.CLEAN)
    vm_probe = FakePreflightProbe(VM_CLEAN)
    queue.preflight_probe = default
    queue.domain_preflight_probes = {"civil-petition": vm_probe}
    drift_calls = []
    queue.vm_domain_state = lambda: drift_calls.append(1) or "was01 changed since install: petition.war"
    await queue.start()
    calls_before = default.calls
    runner.scenario_metadata_path.write_text("{broken", encoding="utf-8")
    state = await queue.tick()
    assert state.phase == "paused" and runner.started == []
    assert default.calls == calls_before and vm_probe.calls == 0
    assert f"{SCENARIO_METADATA_CHECK_NAME}: scenario metadata unreadable" in (state.reason or "")

    # No entry for the scenario: the same.
    document = {"schema_version": "1.0", "scenarios": {}}
    runner.scenario_metadata_path.write_text(json.dumps(document), encoding="utf-8")
    await queue.resume()  # readiness does not read the entry; the injection gate does, and stops again
    state = await queue.tick()
    assert state.phase == "paused" and "scenario metadata has no entry for F01-R" in (state.reason or "")
    assert runner.started == []


def _make_vm_domain(runner, *scenario_ids: str) -> None:
    """Mark scenarios as civil-petition in the scenario metadata the queue reads."""
    document = json.loads(runner.scenario_metadata_path.read_text())
    for scenario_id in scenario_ids:
        document["scenarios"][scenario_id]["domain"] = "civil-petition"
    runner.scenario_metadata_path.write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.asyncio
async def test_a_vm_domain_scenario_is_judged_on_its_own_signals_and_the_others_keep_r6(tmp_path: Path) -> None:
    from app.live_queue import VM_DOMAIN_STATE_CHECK_NAME

    queue, runner, _, _, clock = make_queue(tmp_path)
    _make_vm_domain(runner, "F01-R")
    default = FakePreflightProbe(FakePreflightProbe.CLEAN)
    vm_probe = FakePreflightProbe(VM_CLEAN)
    queue.preflight_probe = default
    queue.domain_preflight_probes = {"civil-petition": vm_probe}
    pods = []
    queue.domain_pods_probe = lambda: pods.append(1) or "1 of 31 app pods not Ready: rca-testbed-commerce/x(Running)"
    queue.vm_domain_state = lambda: None
    await queue.start()
    calls_before = default.calls  # readiness proved the default probe once
    # The pods gate holds every domain, this one included (review rv-t4r3 M3: a recording holds all of 119).
    state = await queue.tick()
    assert runner.started == [] and pods == [1] and state.phase != "paused"
    queue.domain_pods_probe = lambda: pods.append(1) or None
    clock.value += timedelta(seconds=31)
    state = await queue.tick()
    assert runner.started == [("F01-R", "run")], state.reason
    assert vm_probe.calls == 1 and default.calls == calls_before
    verdict = json.loads((runner.artifact_store.root / "run-F01-R" / "preflight.json").read_text())
    names = {check["name"] for check in verdict["checks"]}
    assert {"tunnel_handshake_age_sec", "mgmt_ssh_settle_sec", "residual_guards", "router_cpu_pct"} <= names
    assert "prev_pool_pending" in names  # U7 rule as is (review L1)
    assert VM_DOMAIN_STATE_CHECK_NAME not in names

    # A scenario of another domain still goes through the R6 probe.
    assert queue._preflight_probe_for("F01-H") == (default, None)


@pytest.mark.asyncio
async def test_a_vm_domain_scenario_without_its_probe_is_never_judged_on_r6(tmp_path: Path) -> None:
    """CIVIL_PETITION_OPS off: the gate has no probe for the domain, waits like a broken probe and then pauses
    with why; it never falls back to the commerce signals."""
    queue, runner, _, _, clock = make_queue(tmp_path)
    _make_vm_domain(runner, "F01-R")
    queue.preflight_probe = FakePreflightProbe(FakePreflightProbe.CLEAN)
    await queue.start()
    for attempt in range(1, PREFLIGHT_MAX_ATTEMPTS):
        state = await queue.tick()
        assert state.phase == "running" and state.preflight_attempts == attempt
        assert "no preflight probe for domain civil-petition" in (state.reason or "")
        clock.value += timedelta(minutes=5)
    state = await queue.tick()
    assert state.phase == "paused" and runner.started == []
    assert "no preflight probe for domain civil-petition" in (state.reason or "")


@pytest.mark.asyncio
async def test_a_vm_domain_dirty_gate_waits_and_names_the_failing_signal(tmp_path: Path) -> None:
    """A tunnel cut on purpose: dirty, rechecked every 5 minutes with the failing values in the reason."""
    queue, runner, _, _, clock = make_queue(tmp_path)
    _make_vm_domain(runner, "F01-R")
    vm_probe = FakePreflightProbe({**VM_CLEAN, "tunnel_handshake_age_sec": 1_000_000, "tunnel_ping_lost": 10})
    queue.preflight_probe = FakePreflightProbe(FakePreflightProbe.CLEAN)
    queue.domain_preflight_probes = {"civil-petition": vm_probe}
    await queue.start()
    state = await queue.tick()
    assert runner.started == [] and state.preflight_attempts == 1
    assert "tunnel_handshake_age_sec=1e+06 (fail, lte 150)" in (state.reason or "")
    assert "tunnel_ping_lost=10 (fail, eq 0)" in (state.reason or "")
    vm_probe.obs = dict(VM_CLEAN)
    clock.value += timedelta(minutes=5)
    await queue.tick()
    assert runner.started == [("F01-R", "run")]


@pytest.mark.asyncio
async def test_vm_domain_drift_pauses_before_the_injection_with_what_differs(tmp_path: Path) -> None:
    """§6-4 버전 표류 (§6-6 live_queue row): a VM that does not run what the install left needs a person, so the
    queue pauses at once with the list (raw text, U3). Only this domain's scenarios are gated by it."""
    from app.live_queue import VM_DOMAIN_STATE_CHECK_NAME

    queue, runner, _, _, clock = make_queue(tmp_path)
    _make_vm_domain(runner, "F01-R")
    queue.preflight_probe = FakePreflightProbe(FakePreflightProbe.CLEAN)
    vm_probe = FakePreflightProbe(VM_CLEAN)
    queue.domain_preflight_probes = {"civil-petition": vm_probe}
    answers = ["was01 /var/lib/petition-deploy/war.sha256 rc=1; was01 changed since install: petition.war"]
    queue.vm_domain_state = lambda: answers[0]
    await queue.start()
    state = await queue.tick()
    assert state.phase == "paused" and runner.started == [] and vm_probe.calls == 0
    assert state.reason == (
        f"operational readiness failed: {VM_DOMAIN_STATE_CHECK_NAME} ({VM_DOMAIN_STATE_CHECK_NAME}: "
        "was01 /var/lib/petition-deploy/war.sha256 rc=1; was01 changed since install: petition.war)"
    )
    answers[0] = None
    await queue.resume()
    await queue.tick()
    assert runner.started == [("F01-R", "run")]

    # A state read that raises also stops the injection, with its words.
    (tmp_path / "b").mkdir()
    queue, runner, _, _, clock = make_queue(tmp_path / "b")
    _make_vm_domain(runner, "F01-R")
    queue.domain_preflight_probes = {"civil-petition": FakePreflightProbe(VM_CLEAN)}

    def broken():
        raise RuntimeError("helper checksum command exited 2: no such module")

    queue.vm_domain_state = broken
    await queue.start()
    state = await queue.tick()
    assert state.phase == "paused" and "helper checksum command exited 2: no such module" in (state.reason or "")

    # A commerce scenario is not gated by the VM state.
    (tmp_path / "c").mkdir()
    queue, runner, _, _, clock = make_queue(tmp_path / "c")
    queue.preflight_probe = FakePreflightProbe(FakePreflightProbe.CLEAN)
    queue.vm_domain_state = lambda: "rt-core01 snmpd.service inactive"
    await queue.start()
    await queue.tick()
    assert runner.started == [("F01-R", "run")]


def test_compose_carries_the_vm_testbed_settings_and_leaves_them_off() -> None:
    compose = (Path(__file__).resolve().parents[2] / "docker-compose.yml").read_text(encoding="utf-8")
    assert "- CIVIL_PETITION_OPS=${CIVIL_PETITION_OPS:-}" in compose
    # The 66 target is fixed in code (the executors name the same socket), not a setting (review L2).
    assert "VM_OPS_HYPERVISOR_TARGET" not in compose
    # Room to end the masters with -O exit before docker's SIGKILL (review H1).
    assert "    stop_grace_period: 60s\n" in compose
    assert "- VM_OPS_STATE_DIR=/app/state/vm-ops" in compose
    # The socket directory the executors read from the same variable (T4 contract 3), on the state volume.
    assert "- CIVIL_PETITION_SSH_CONTROL_DIR=/app/state/ssh-control" in compose
    # Keys and known_hosts through one directory (a missing single file would become a directory on the host).
    assert "- ${VM_OPS_KEY_HOST_DIR:-/root/.ssh/vm-ops}:/root/.ssh/vm-ops:ro" in compose
    assert (
        "- ${CIVIL_PETITION_DEPLOY_HOST_PATH:-../testbed-services/civil-petition/deploy}"
        ":/opt/lucida/civil-petition-deploy:ro"
    ) in compose
    assert "- CIVIL_PETITION_TARGETS_PATH=/opt/lucida/civil-petition-deploy/inventory/t.targets.json" in compose


# --- S testbed (civil-petition-sms, ST4) --------------------------------------------------------------------------
from tests.test_preflight import SMS_CLEAN  # noqa: E402


def _make_domain(runner, domain: str, *scenario_ids: str) -> None:
    document = json.loads(runner.scenario_metadata_path.read_text())
    for scenario_id in scenario_ids:
        document["scenarios"][scenario_id]["domain"] = domain
    runner.scenario_metadata_path.write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.asyncio
async def test_an_s_scenario_uses_s_s_signals_s_drift_gate_and_records_the_pair(tmp_path: Path) -> None:
    """S design §7-4, §7-6 live_queue row: an S scenario is judged on S's own signal set and drift gate (never T's),
    and its run keeps pair.json (whether T and S run the same software; never a gate)."""
    from app.live_queue import VM_DOMAIN_STATE_CHECK_NAME

    queue, runner, _, _, clock = make_queue(tmp_path)
    _make_domain(runner, "civil-petition-sms", "F01-R")
    s_probe, t_probe = FakePreflightProbe(SMS_CLEAN), FakePreflightProbe(VM_CLEAN)
    queue.domain_preflight_probes = {"civil-petition": t_probe, "civil-petition-sms": s_probe}
    t_drift, s_drift = [], []
    queue.vm_domain_state = lambda: t_drift.append(1) or "web01 changed since install: /etc/x"
    answers = ["dbsvr01 postgresql@16-main.service inactive"]
    queue.vm_domain_states = {"civil-petition-sms": lambda: s_drift.append(1) or answers[0]}
    pair = {"pair": False, "differences": ["websvr01/web01: normalized configuration checksums differ"]}
    queue.vm_pair_state = lambda: dict(pair)
    await queue.start()
    state = await queue.tick()
    assert state.phase == "paused" and runner.started == [] and t_drift == [] and s_drift == [1]
    assert f"{VM_DOMAIN_STATE_CHECK_NAME}: dbsvr01 postgresql@16-main.service inactive" in (state.reason or "")
    answers[0] = None
    await queue.resume()
    state = await queue.tick()
    assert runner.started == [("F01-R", "run")], state.reason
    assert s_probe.calls == 1 and t_probe.calls == 0 and t_drift == []
    recorded = json.loads((runner.artifact_store.root / "run-F01-R" / "pair.json").read_text())
    assert recorded == {**pair, "domain": "civil-petition-sms"}
    verdict = json.loads((runner.artifact_store.root / "run-F01-R" / "preflight.json").read_text())
    assert not [c for c in verdict["checks"] if c["name"].startswith("router_")]


@pytest.mark.asyncio
async def test_the_pair_record_is_kept_for_vm_testbed_runs_only_and_says_why_when_unreadable(tmp_path: Path) -> None:
    queue, runner, _, _, clock = make_queue(tmp_path)
    queue.vm_pair_state = lambda: {"pair": True}
    await queue.start()
    state = await queue.tick()
    assert runner.started == [("F01-R", "run")]
    assert not (runner.artifact_store.root / "run-F01-R" / "pair.json").exists()  # a Kubernetes domain: no pair
    queue._write_vm_pair_state("run-x", None)
    assert not (runner.artifact_store.root / "run-x" / "pair.json").exists()
    _make_domain(runner, "civil-petition", "F01-R")

    def broken():
        raise RuntimeError("no fresh read")

    queue.vm_pair_state = broken
    queue._write_vm_pair_state("run-y", "F01-R")
    recorded = json.loads((runner.artifact_store.root / "run-y" / "pair.json").read_text())
    assert recorded["pair"] is False and "no fresh read" in recorded["differences"][0]
    assert recorded["domain"] == "civil-petition"


def test_the_s_archive_state_is_its_own_check_over_the_whole_queue(tmp_path: Path) -> None:
    """S design §7-4 보관 배치 상태: the same verdict on S's dbsvr01 facts, its own check name, and like T's it holds
    the whole queue (a recording holds 119 data of every domain)."""
    from app.live_queue import ARCHIVE_STATE_SMS_CHECK_NAME

    queue, runner, _, _, clock = make_queue(tmp_path)
    assert ARCHIVE_STATE_SMS_CHECK_NAME not in queue.readiness().checks
    (tmp_path / "t").mkdir()
    (tmp_path / "s").mkdir()
    t_dir = _archive_facts(tmp_path / "t", clock.value)
    s_dir = _archive_facts(tmp_path / "s", clock.value, rows=2_400_000)
    queue.archive_state_dir, queue.sms_archive_state_dir = t_dir, s_dir
    readiness = queue.readiness()
    assert readiness.checks[ARCHIVE_STATE_CHECK_NAME] is True
    assert readiness.missing == [ARCHIVE_STATE_SMS_CHECK_NAME]
    assert "rows" in readiness.details[ARCHIVE_STATE_SMS_CHECK_NAME]
    _archive_facts(tmp_path / "s", clock.value)
    assert queue.readiness().ready
    queue._write_archive_state("run-z")
    folder = runner.artifact_store.root / "run-z"
    assert json.loads((folder / "archive_state.json").read_text())["passes"] is True
    assert json.loads((folder / "archive_state_sms.json").read_text())["facts"]["host"] == "db01"


def test_the_pair_record_excuses_what_held_decoys_left(tmp_path: Path) -> None:
    """A WAR decoy held through the recording changed the WAR on its node; the pair record judges the testbeds with
    that file excused on that node, as the drift gate does (ST4 contract 3)."""
    from app.decoys import DecoyRecord

    queue, runner, _, _, clock = make_queue(tmp_path)
    _make_domain(runner, "civil-petition-sms", "F01-R")
    asked = []
    queue.vm_pair_state = lambda **kwargs: asked.append(kwargs) or {"pair": True}
    held = DecoyRecord(id="d1", window="normal", planned_at="2026-11-10T01:30:00Z", kind="war_redeploy",
                       namespace="civil-petition-sms-vm", deployment="petition-wassvr01", node="wassvr01",
                       service_name="wassvr01", target_id="t", relation="same_domain", value="x", status="held",
                       changed_paths=["/var/lib/tomcat10/webapps/petition.war"])
    queue._write_vm_pair_state("run-h", "F01-R", [held])
    assert asked == [{"excused": {"wassvr01": {"paths": {"/var/lib/tomcat10/webapps/petition.war"}, "guards": 1,
                                               "ids": ["d1"]}}}]
    queue._write_vm_pair_state("run-n", "F01-R", [])
    assert asked[-1] == {}
