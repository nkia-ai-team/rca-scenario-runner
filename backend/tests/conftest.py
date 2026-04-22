import asyncio
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import runner as runner_module
from app.main import app
from app.scenarios import SCENARIOS


@pytest.fixture
def fake_scripts(tmp_path: Path) -> Path:
    """
    Create stub bash scripts for each scenario id.
    - no arg: echoes a few lines and exits 0
    - 'cleanup' arg: echoes cleanup line and exits 0
    """
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    for scenario in SCENARIOS.values():
        p = script_dir / scenario.script_filename
        p.write_text(
            """#!/bin/bash
if [ "${1:-}" = "cleanup" ]; then
  echo "[OK] cleanup complete for $(basename $0)"
  exit 0
fi
echo "[INFO] starting $(basename $0)"
echo "[INFO] stage 1"
echo "[INFO] stage 2"
echo "[OK] done"
exit 0
"""
        )
        p.chmod(0o755)
    return script_dir


@pytest_asyncio.fixture
async def client(fake_scripts: Path, tmp_path: Path) -> AsyncClient:
    log_dir = tmp_path / "logs"
    runner_module.reset_runner_for_tests(script_dir=fake_scripts, log_dir=log_dir)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def wait_until_idle(client: AsyncClient, scenario_id: str, timeout: float = 5.0) -> dict:
    """Poll status until status leaves running/cleanup_running, or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"/api/scenarios/{scenario_id}/status")
        if resp.status_code == 200:
            data = resp.json()
            if data["status"] not in {"running", "cleanup_running"}:
                return data
        await asyncio.sleep(0.1)
    raise TimeoutError(f"Scenario {scenario_id} did not complete within {timeout}s")
