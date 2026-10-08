import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Body, FastAPI, HTTPException, Response
from pydantic import BaseModel
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.models import ActiveRun, Domain, HealthResponse, HistoryEntry, RunInfo, Scenario
from app.runner import get_runner
from app.scenarios import get_scenario, list_domains, list_scenarios
from app.coordinator import get_coordinator
from app.manifests import ScenarioManifest, get_manifest, load_manifests
from app.live_catalog import domains_of, list_live_scenarios
from app.watchdog import WatchdogDecision, WatchdogRequest, decide_watchdog
from app.live_queue import LiveQueueState, OperationalReadiness, get_live_queue
from app.pass_mode import isolation_checks_enabled

@asynccontextmanager
async def lifespan(_: FastAPI):
    runner = get_runner()
    runner.ensure_capture_worker()
    runner.ensure_watchdog_worker()
    get_live_queue().ensure_worker()
    try:
        yield
    finally:
        await get_live_queue().stop_worker()
        await runner.stop_capture_worker()
        await runner.stop_watchdog_worker()


app = FastAPI(
    title="RCA Testbed Scenario Runner",
    version="0.1.0",
    description="Internal web UI backend for triggering RCA testbed failure scenarios",
    lifespan=lifespan,
)


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok")


def _live_scenarios() -> list[Scenario]:
    """Current manifest-backed catalog; empty when the deployment mounts none."""
    return list_live_scenarios(get_runner().scenario_metadata_path)


@app.get("/api/scenarios", response_model=list[Scenario])
async def api_list_scenarios() -> list[Scenario]:
    # The UI lists what runs today. The in-repo legacy catalog (earlier testbed
    # generations) is only the fallback for deployments without live manifests;
    # its ids stay resolvable through the routes below either way.
    return _live_scenarios() or list_scenarios()


@app.get("/api/scenario-manifests", response_model=list[ScenarioManifest])
async def api_list_scenario_manifests() -> list[ScenarioManifest]:
    """List the external 64-scenario catalog without duplicating alias keys."""
    unique = {manifest.slug: manifest for manifest in load_manifests().values()}
    return [unique[key] for key in sorted(unique)]


@app.get("/api/scenario-manifests/{scenario_id}", response_model=ScenarioManifest)
async def api_get_scenario_manifest(scenario_id: str) -> ScenarioManifest:
    manifest = get_manifest(scenario_id)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"Scenario manifest {scenario_id} not found")
    return manifest


@app.get("/api/domains", response_model=list[Domain])
async def api_list_domains() -> list[Domain]:
    live = _live_scenarios()
    return domains_of(live) if live else list_domains()


@app.get("/api/active", response_model=ActiveRun)
async def api_active() -> ActiveRun:
    """Global snapshot — any client polls this to know if anyone else is busy."""
    return get_runner().get_active()


@app.get("/api/live-queue", response_model=LiveQueueState)
async def api_live_queue() -> LiveQueueState:
    return get_live_queue().snapshot()


@app.get("/api/live-queue/readiness", response_model=OperationalReadiness)
async def api_live_queue_readiness() -> OperationalReadiness:
    # readiness() shells out to the capture self-check (up to 120s). Run on a
    # thread: served inline it froze the event loop, starving the coordinator
    # heartbeat until the active run's 30s lease expired and every subsequent
    # operation died on fencing rejection (#31, 2026-08-03 batch — F15-T2).
    return await asyncio.to_thread(get_live_queue().readiness)


class LiveQueueStartRequest(BaseModel):
    # Optional cycle-mode subset for this queue only (approved ids). Omitted or
    # no body keeps the configured CYCLE_SCENARIOS / whole approved list.
    scenario_ids: list[str] | None = None


@app.post("/api/live-queue/start", response_model=LiveQueueState)
async def api_live_queue_start(
    body: LiveQueueStartRequest | None = Body(default=None),
) -> LiveQueueState:
    try:
        return await get_live_queue().start(scenario_ids=body.scenario_ids if body else None)
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/live-queue/resume", response_model=LiveQueueState)
async def api_live_queue_resume() -> LiveQueueState:
    try:
        return await get_live_queue().resume()
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/live-queue/append-promoted", response_model=LiveQueueState)
async def api_live_queue_append_promoted() -> LiveQueueState:
    try:
        return await get_live_queue().append_promoted()
    except RuntimeError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@app.post("/api/controller/watchdog/decision", response_model=WatchdogDecision)
async def api_watchdog_decision(request: WatchdogRequest) -> WatchdogDecision:
    """Read-only decision endpoint. It never claims cleanup or launches commands."""
    return decide_watchdog(
        get_coordinator().snapshot(),
        now=request.now,
        heartbeat_timeout_sec=request.heartbeat_timeout_sec,
    )


@app.get("/api/scenarios/{scenario_id}", response_model=Scenario)
async def api_get_scenario(scenario_id: str) -> Scenario:
    scenario = get_scenario(scenario_id)
    if scenario is None:
        scenario = next((s for s in _live_scenarios() if s.id == scenario_id), None)
    if scenario is None:
        raise HTTPException(status_code=404, detail=f"Scenario {scenario_id} not found")
    return scenario


@app.post("/api/scenarios/{scenario_id}/run", response_model=RunInfo)
async def api_run(scenario_id: str) -> RunInfo:
    runner = get_runner()
    try:
        # 큐(live_queue.py:702)와 같은 판단을 쓴다. 이 엔드포인트만 pass mode 를
        # 읽지 않아, smoke pass 에서 큐는 통과하는 런이 단독 실행으로는
        # check_failed:clean-window 로 거부됐다 — 같은 시나리오·같은 클러스터·같은
        # 순간인데 진입 경로에 따라 답이 갈렸다. isolation 게이트는 capture 창 쌍을
        # 떼어놓기 위한 것이고 smoke pass 에는 그 쌍이 없다(pass_mode.py:82-87).
        #
        # 2026-08-12 에 이걸로 4종 단독 재실행이 통째로 막혔다. 겸사겸사 드러난 것:
        # 08-07~09 의 F15-T2 런 넷이 status=dirty / effect_ended=None 으로 닫히지
        # 않아 227 개 런의 overlapping_run_ids 에 영구히 들어간다. 게이트가 켜져
        # 있었더라도 아무도 통과하지 못했을 상태다(별건으로 남긴다).
        return await runner.start(
            scenario_id=scenario_id,
            mode="run",
            skip_isolation_checks=not isolation_checks_enabled(),
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/scenarios/{scenario_id}/cleanup", response_model=RunInfo)
async def api_cleanup(scenario_id: str, repair_capsule: bool = False) -> RunInfo:
    """Clean up a DIRTY run.

    `repair_capsule` re-cuts the run's frozen contract tree from the live trusted
    root before cleaning. Use it when the capsule's own executor is the defect —
    otherwise the run cannot clean itself, and since DIRTY is global that blocks
    every scenario. The run's plan is left untouched and re-verified, and the
    swap is recorded in `capsule-repair.json`. Cleanup and recovery must still
    pass on their own merits; this changes what code runs, not what counts as
    clean.
    """
    runner = get_runner()
    try:
        return await runner.start(
            scenario_id=scenario_id, mode="cleanup", repair_capsule=repair_capsule
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/scenarios/{scenario_id}/dry-run")
async def api_dry_run(
    scenario_id: str,
    mode: Literal["run", "cleanup"] = "run",
) -> dict:
    try:
        return get_runner().dry_run(scenario_id=scenario_id, mode=mode)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/scenarios/{scenario_id}/status", response_model=RunInfo)
async def api_status(scenario_id: str) -> RunInfo:
    runner = get_runner()
    current = runner.get_current()
    # Resolve bare short_id (legacy) to composite before comparing — current.scenario_id is always composite.
    resolved = get_scenario(scenario_id)
    canonical_id = resolved.id if resolved is not None else scenario_id
    if current is None or current.scenario_id != canonical_id:
        raise HTTPException(
            status_code=404,
            detail=f"No active or recent run for scenario {scenario_id}",
        )
    return current


@app.get("/api/scenarios/{scenario_id}/logs", response_class=PlainTextResponse)
async def api_full_log(scenario_id: str, run_id: str) -> Response:
    runner = get_runner()
    log_file = runner.log_path(run_id)
    if not log_file.exists():
        raise HTTPException(status_code=404, detail=f"Log not found for run {run_id}")
    return PlainTextResponse(log_file.read_text(encoding="utf-8"))


@app.get("/api/history", response_model=list[HistoryEntry])
async def api_history() -> list[HistoryEntry]:
    return get_runner().get_history()


# Static frontend (production). Mount LAST so /api/* routes above win.
_STATIC_DIR = Path(os.environ.get("STATIC_DIR", "/app/static"))
if _STATIC_DIR.is_dir():
    app.mount(
        "/",
        StaticFiles(directory=str(_STATIC_DIR), html=True),
        name="static",
    )
