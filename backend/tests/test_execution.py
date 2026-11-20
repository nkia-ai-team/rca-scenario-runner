from pathlib import Path

import pytest
from pydantic import ValidationError

from app.execution import build_invocation
from app.execution_policy import validate_injection_policy
from app.models import ExecutionPlan, ExecutionSpec, InjectionPoint
from app.runner import ScenarioRunner
from app.scenarios import SCENARIOS


@pytest.fixture
def script(tmp_path: Path) -> Path:
    path = tmp_path / "scenario.sh"
    path.write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8")
    return path


def test_local_invocation_preserves_legacy_behavior(script: Path) -> None:
    spec = ExecutionSpec(transport="local", location="runner")
    invocation = build_invocation(spec, script, "commerce:01", "cleanup")
    assert invocation.argv == ["bash", str(script), "cleanup"]
    assert invocation.stdin_bytes is None


def test_ssh_invocation_streams_central_script(script: Path) -> None:
    spec = ExecutionSpec(
        transport="ssh",
        location="tb-runner",
        host="192.168.122.206",
        user="root",
        identity_file="/root/.ssh/tb_key",
    )
    invocation = build_invocation(spec, script, "commerce:01", "run")
    assert invocation.argv[-4:] == ["root@192.168.122.206", "bash", "-s", "--"]
    assert invocation.stdin_bytes == script.read_bytes()


def test_docker_invocation_streams_central_script(script: Path) -> None:
    spec = ExecutionSpec(transport="docker", location="postgres", container="testbed-postgres")
    invocation = build_invocation(spec, script, "commerce:01", "cleanup")
    assert invocation.argv == [
        "docker", "exec", "-i", "testbed-postgres", "bash", "-s", "--", "cleanup"
    ]
    assert invocation.stdin_bytes == script.read_bytes()


def test_kubectl_invocation_streams_central_script(script: Path) -> None:
    spec = ExecutionSpec(
        transport="kubectl",
        location="commerce postgres",
        namespace="rca-testbed-commerce",
        resource="pod/testbed-postgres-0",
    )
    invocation = build_invocation(spec, script, "commerce:01", "cleanup")
    assert invocation.argv == [
        "kubectl", "--namespace", "rca-testbed-commerce", "exec", "-i",
        "pod/testbed-postgres-0", "--", "bash", "-s", "--", "cleanup",
    ]
    assert invocation.stdin_bytes == script.read_bytes()


def test_api_invocation_uses_cleanup_url_and_json_payload(
    script: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAULT_API_TOKEN", "test-token")
    spec = ExecutionSpec(
        transport="api",
        location="fault-api",
        url="http://fault-api/run",
        cleanup_url="http://fault-api/cleanup",
        header_env={"Authorization": "FAULT_API_TOKEN"},
    )
    invocation = build_invocation(spec, script, "commerce:01", "cleanup")
    assert invocation.argv == ["curl", "--config", "-"]
    assert invocation.stdin_bytes is not None
    config = invocation.stdin_bytes.decode()
    assert 'url = "http://fault-api/cleanup"' in config
    assert '{\\"scenario_id\\":\\"commerce:01\\",\\"mode\\":\\"cleanup\\"}' in config
    assert 'header = "Authorization: test-token"' in config
    assert "test-token" not in invocation.argv


def test_api_invocation_fails_closed_when_header_secret_is_missing(script: Path) -> None:
    spec = ExecutionSpec(
        transport="api",
        location="fault-api",
        url="http://fault-api/run",
        header_env={"Authorization": "MISSING_FAULT_API_TOKEN"},
    )
    with pytest.raises(ValueError, match="MISSING_FAULT_API_TOKEN"):
        build_invocation(spec, script, "commerce:01", "run")


@pytest.mark.parametrize(
    ("transport", "message"),
    [
        ("ssh", "execution.host"),
        ("docker", "execution.container"),
        ("kubectl", "execution.namespace"),
        ("api", "execution.url"),
    ],
)
def test_transport_requires_its_target(transport: str, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        ExecutionSpec(transport=transport, location="missing")


def test_policy_rejects_north_south_from_cluster() -> None:
    point = InjectionPoint(
        id="surge",
        kind="north_south",
        transport="kubectl",
        location="commerce pod",
        namespace="rca-testbed-commerce",
        resource="deployment/api-gateway",
        target="gateway",
        entry_path="pod to service",
        cleanup_location="commerce pod",
        rationale="invalid test fixture",
        feasibility="ready",
    )
    with pytest.raises(ValueError, match="north_south"):
        validate_injection_policy(point)


async def test_dispatch_preparation_failure_marks_run_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script_path = tmp_path / "scenario.sh"
    script_path.write_text("#!/usr/bin/env bash\necho should-not-run\n", encoding="utf-8")
    scenario = next(iter(SCENARIOS.values())).model_copy(
        update={
            "script_filename": script_path.name,
            "execution": ExecutionPlan(
                orchestrator=ExecutionSpec(
                    transport="api",
                    location="fault-api",
                    url="http://fault-api/run",
                    header_env={"Authorization": "MISSING_RUNNER_TOKEN"},
                )
            ),
        }
    )
    monkeypatch.setattr("app.runner.get_scenario", lambda _: scenario)
    runner = ScenarioRunner(script_dir=tmp_path, log_dir=tmp_path / "logs")

    await runner.start(scenario.id, "run")
    assert runner._task is not None
    await runner._task

    current = runner.get_current()
    assert current is not None
    assert current.status == "failed"
    assert current.exit_code == -1
    assert any("MISSING_RUNNER_TOKEN" in line for line in current.log_tail)


async def test_a_timed_out_script_is_ended_with_everything_it_started(tmp_path: Path) -> None:
    """Review rv-t4o M2: a timeout used to kill only bash, and an ssh it had started stayed behind (on a VM testbed
    that ssh keeps the VM's operator lock and a session that never closes). The script runs in its own process
    group and the whole group is ended."""
    import os

    pid_file = tmp_path / "child.pid"
    script = tmp_path / "hang.sh"
    script.write_text(f"#!/usr/bin/env bash\nsleep 60 &\necho $! > {pid_file}\nwait\n", encoding="utf-8")
    runner = ScenarioRunner(script_dir=tmp_path, log_dir=tmp_path / "logs")
    import time as _time

    started = _time.monotonic()
    with open(tmp_path / "log.txt", "w", encoding="utf-8") as log_file:
        code = await runner._invoke_once(
            ExecutionSpec(transport="local", location="runner", timeout_sec=1), script, "F99-X", "run", log_file
        )
    # Ended with its group, not when the 60-second child happened to finish by itself.
    assert code == -1 and _time.monotonic() - started < 20
    child = int(pid_file.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        import time

        time.sleep(0.1)
    else:
        os.kill(child, 9)
        raise AssertionError("the script's child outlived the timeout")
