import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.models import HealthResponse, HistoryEntry, RunInfo, Scenario
from app.runner import get_runner
from app.scenarios import get_scenario, list_scenarios

app = FastAPI(
    title="RCA Testbed Scenario Runner",
    version="0.1.0",
    description="Internal web UI backend for triggering RCA testbed failure scenarios",
)


@app.get("/healthz", response_model=HealthResponse)
async def healthz() -> HealthResponse:
    return HealthResponse(status="ok")


@app.get("/api/scenarios", response_model=list[Scenario])
async def api_list_scenarios() -> list[Scenario]:
    return list_scenarios()


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


@app.get("/api/scenarios/{scenario_id}/status", response_model=RunInfo)
async def api_status(scenario_id: str) -> RunInfo:
    runner = get_runner()
    current = runner.get_current()
    if current is None or current.scenario_id != scenario_id:
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
