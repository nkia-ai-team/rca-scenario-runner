import pytest
from httpx import AsyncClient

from tests.conftest import wait_until_idle


async def test_healthz(client: AsyncClient) -> None:
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "version": "0.1.0"}


async def test_list_scenarios_returns_plopvape_four(client: AsyncClient) -> None:
    resp = await client.get("/api/scenarios")
    assert resp.status_code == 200
    data = resp.json()
    plopvape = [s for s in data if s["domain"] == "plopvape-shop"]
    assert len(plopvape) == 4
    assert {s["short_id"] for s in plopvape} == {"01", "02", "03", "04"}
    assert {s["id"] for s in plopvape} == {
        "plopvape-shop:01",
        "plopvape-shop:02",
        "plopvape-shop:03",
        "plopvape-shop:04",
    }


async def test_get_single_scenario(client: AsyncClient) -> None:
    # Bare short_id resolves to DEFAULT_DOMAIN (plopvape-shop) for backward compat
    resp = await client.get("/api/scenarios/01")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Inventory Row Lock Contention"
    assert body["id"] == "plopvape-shop:01"
    assert body["domain"] == "plopvape-shop"


async def test_list_domains(client: AsyncClient) -> None:
    resp = await client.get("/api/domains")
    assert resp.status_code == 200
    data = resp.json()
    slugs = {d["slug"] for d in data}
    assert "plopvape-shop" in slugs
    plopvape = next(d for d in data if d["slug"] == "plopvape-shop")
    assert plopvape["scenario_count"] == 4


async def test_plopvape_scenarios_have_rca_ground_truth(client: AsyncClient) -> None:
    """plopvape 4 시나리오 모두 difficulty + expected_rca_root_cause 채워져 RCA 채점 가능해야 한다."""
    resp = await client.get("/api/scenarios")
    plopvape = [s for s in resp.json() if s["domain"] == "plopvape-shop"]
    assert len(plopvape) == 4
    for s in plopvape:
        assert isinstance(s["difficulty"], int) and 1 <= s["difficulty"] <= 5, (
            f"{s['id']} difficulty missing or out of range"
        )
        assert isinstance(s["expected_rca_root_cause"], str), (
            f"{s['id']} expected_rca_root_cause missing"
        )
        assert len(s["expected_rca_root_cause"].strip()) > 50, (
            f"{s['id']} expected_rca_root_cause too short — observable signals 부족"
        )


async def test_get_unknown_scenario_returns_404(client: AsyncClient) -> None:
    resp = await client.get("/api/scenarios/99")
    assert resp.status_code == 404


async def test_run_scenario_end_to_end(client: AsyncClient) -> None:
    resp = await client.post("/api/scenarios/01/run")
    assert resp.status_code == 200
    run = resp.json()
    assert run["status"] == "running"
    # Bare "01" resolves to composite "plopvape-shop:01" inside the runner
    assert run["scenario_id"] == "plopvape-shop:01"
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


async def test_active_endpoint_reflects_busy_state(client: AsyncClient) -> None:
    idle = await client.get("/api/active")
    assert idle.status_code == 200
    assert idle.json()["is_active"] is False

    await client.post("/api/scenarios/01/run")
    busy = await client.get("/api/active")
    assert busy.status_code == 200
    body = busy.json()
    assert body["is_active"] is True
    assert body["scenario_id"] == "plopvape-shop:01"
    assert body["mode"] == "run"

    await wait_until_idle(client, scenario_id="01")
    after = await client.get("/api/active")
    assert after.json()["is_active"] is False


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
    assert entries[0]["scenario_id"] == "plopvape-shop:03"
    assert entries[0]["status"] == "succeeded"


async def test_full_log_download(client: AsyncClient) -> None:
    start = await client.post("/api/scenarios/04/run")
    run_id = start.json()["run_id"]
    await wait_until_idle(client, scenario_id="04")

    log = await client.get(f"/api/scenarios/04/logs", params={"run_id": run_id})
    assert log.status_code == 200
    assert "starting" in log.text
    assert "[OK] done" in log.text
