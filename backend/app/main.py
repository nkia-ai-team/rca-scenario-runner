import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.models import ActiveRun, Domain, HealthResponse, HistoryEntry, RunInfo, Scenario
from app.runner import get_runner
from app.scenarios import get_scenario, list_domains, list_scenarios
from app.coordinator import get_coordinator
from app.manifests import ScenarioManifest, get_manifest, load_manifests
from app.watchdog import WatchdogDecision, WatchdogRequest, decide_watchdog
from app.live_queue import LiveQueueState, OperationalReadiness, get_live_queue

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


@app.get("/api/scenarios", response_model=list[Scenario])
async def api_list_scenarios() -> list[Scenario]:
    return list_scenarios()


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
    return list_domains()


@app.get("/api/active", response_model=ActiveRun)
async def api_active() -> ActiveRun:
    """Global snapshot — any client polls this to know if anyone else is busy."""
    return get_runner().get_active()


@app.get("/api/live-queue", response_model=LiveQueueState)
async def api_live_queue() -> LiveQueueState:
    return get_live_queue().snapshot()


@app.get("/api/live-queue/readiness", response_model=OperationalReadiness)
async def api_live_queue_readiness() -> OperationalReadiness:
    return get_live_queue().readiness()


@app.post("/api/live-queue/start", response_model=LiveQueueState)
async def api_live_queue_start() -> LiveQueueState:
    try:
        return await get_live_queue().start()
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
        raise HTTPException(status_code=404, detail=f"Scenario {scenario_id} not found")
    return scenario


@app.post("/api/scenarios/{scenario_id}/run", response_model=RunInfo)
async def api_run(scenario_id: str) -> RunInfo:
    runner = get_runner()
    try:
        return await runner.start(scenario_id=scenario_id, mode="run")
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/api/scenarios/{scenario_id}/cleanup", response_model=RunInfo)
async def api_cleanup(scenario_id: str) -> RunInfo:
    runner = get_runner()
    try:
        return await runner.start(scenario_id=scenario_id, mode="cleanup")
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
