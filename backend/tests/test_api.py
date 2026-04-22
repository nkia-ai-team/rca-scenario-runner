import pytest
from httpx import AsyncClient

from tests.conftest import wait_until_idle


async def test_healthz(client: AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": "0.1.0"}


async def test_list_scenarios_returns_four(client: AsyncClient) -> None:
    resp = await client.get("/api/scenarios")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 4
    assert {s["id"] for s in data} == {"01", "02", "03", "04"}


async def test_get_single_scenario(client: AsyncClient) -> None:
    resp = await client.get("/api/scenarios/01")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Inventory Row Lock Contention"


async def test_get_unknown_scenario_returns_404(client: AsyncClient) -> None:
    resp = await client.get("/api/scenarios/99")
    assert resp.status_code == 404


async def test_run_scenario_end_to_end(client: AsyncClient) -> None:
    resp = await client.post("/api/scenarios/01/run")
    assert resp.status_code == 200
    run = resp.json()
    assert run["status"] == "running"
    assert run["scenario_id"] == "01"
    assert run["mode"] == "run"

    final = await wait_until_idle(client, scenario_id="01")
    assert final["status"] == "succeeded"
    assert final["exit_code"] == 0
    assert any("[OK] done" in line for line in final["log_tail"])


async def test_concurrent_run_returns_conflict(client: AsyncClient) -> None:
    first = await client.post("/api/scenarios/01/run")
    assert first.status_code == 200
    # Immediately try another run — should conflict while first is still active
    second = await client.post("/api/scenarios/02/run")
    assert second.status_code == 409, second.text
    # Drain the first to keep test isolated
    await wait_until_idle(client, scenario_id="01")


async def test_cleanup_mode(client: AsyncClient) -> None:
    resp = await client.post("/api/scenarios/02/cleanup")
    assert resp.status_code == 200
    assert resp.json()["mode"] == "cleanup"
    final = await wait_until_idle(client, scenario_id="02")
    assert final["status"] == "succeeded"
    assert any("cleanup complete" in line for line in final["log_tail"])


async def test_history_records_completed_run(client: AsyncClient) -> None:
    await client.post("/api/scenarios/03/run")
    await wait_until_idle(client, scenario_id="03")
    hist = await client.get("/api/history")
    assert hist.status_code == 200
    entries = hist.json()
    assert len(entries) >= 1
    assert entries[0]["scenario_id"] == "03"
    assert entries[0]["status"] == "succeeded"


async def test_full_log_download(client: AsyncClient) -> None:
    start = await client.post("/api/scenarios/04/run")
    run_id = start.json()["run_id"]
    await wait_until_idle(client, scenario_id="04")

    log = await client.get(f"/api/scenarios/04/logs", params={"run_id": run_id})
    assert log.status_code == 200
    assert "starting" in log.text
    assert "[OK] done" in log.text
