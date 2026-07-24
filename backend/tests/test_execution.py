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
