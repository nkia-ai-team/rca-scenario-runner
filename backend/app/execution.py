"""Declarative scenario execution transport builder.

This module only builds subprocess invocations. The runner owns lifecycle,
output streaming, timeouts, and status recording.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.models import ExecutionSpec


@dataclass(frozen=True)
class ExecutionInvocation:
    argv: list[str]
    stdin_bytes: bytes | None = None


def describe_invocation(
    spec: ExecutionSpec,
    script_path: Path,
    scenario_id: str,
    mode: Literal["run", "cleanup"],
) -> dict:
    """Return a redacted, side-effect-free invocation description."""
    mode_arg = _mode_arg(mode)
    if spec.transport == "local":
        argv = ["bash", str(script_path), *mode_arg]
    elif spec.transport == "ssh":
        destination = f"{spec.user}@{spec.host}" if spec.user else str(spec.host)
        argv = ["ssh", destination, "bash", "-s", "--", *mode_arg]
    elif spec.transport == "docker":
        argv = ["docker", "exec", "-i", str(spec.container), "bash", "-s", "--", *mode_arg]
    elif spec.transport == "kubectl":
        argv = [
            "kubectl", "--namespace", str(spec.namespace), "exec", "-i",
            str(spec.resource), "--", "bash", "-s", "--", *mode_arg,
        ]
    else:
        url = spec.cleanup_url if mode == "cleanup" and spec.cleanup_url else spec.url
        argv = ["curl", "--config", "-", str(url)]
    return {
        "transport": spec.transport,
        "location": spec.location,
        "argv": argv,
        "script_via_stdin": spec.transport in {"ssh", "docker", "kubectl"},
        "required_secret_env": sorted(set(spec.header_env.values())),
        "script_sha256": hashlib.sha256(script_path.read_bytes()).hexdigest(),
        "scenario_id": scenario_id,
        "mode": mode,
    }


def _mode_arg(mode: Literal["run", "cleanup"]) -> list[str]:
    return ["cleanup"] if mode == "cleanup" else []


def _curl_config_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def build_invocation(
    spec: ExecutionSpec,
    script_path: Path,
    scenario_id: str,
    mode: Literal["run", "cleanup"],
) -> ExecutionInvocation:
    """Build a shell-free argv for the declared transport."""
    mode_arg = _mode_arg(mode)

    if spec.transport == "local":
        return ExecutionInvocation(["bash", str(script_path), *mode_arg])

    if spec.transport == "ssh":
        argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=10",
            "-p",
            str(spec.port),
        ]
        if spec.identity_file:
            argv.extend(["-i", spec.identity_file])
        destination = f"{spec.user}@{spec.host}" if spec.user else str(spec.host)
        argv.extend([destination, "bash", "-s", "--", *mode_arg])
        return ExecutionInvocation(argv, script_path.read_bytes())

    if spec.transport == "docker":
        argv = ["docker", "exec", "-i", str(spec.container), "bash", "-s", "--", *mode_arg]
        return ExecutionInvocation(argv, script_path.read_bytes())

    if spec.transport == "kubectl":
        argv = [
            "kubectl",
            "--namespace",
            str(spec.namespace),
            "exec",
            "-i",
            str(spec.resource),
            "--",
            "bash",
            "-s",
            "--",
            *mode_arg,
        ]
        return ExecutionInvocation(argv, script_path.read_bytes())

    url = spec.cleanup_url if mode == "cleanup" and spec.cleanup_url else spec.url
    payload = json.dumps(
        {"scenario_id": scenario_id, "mode": mode},
        separators=(",", ":"),
    )
    config_lines = [
        "fail-with-body",
        "silent",
        "show-error",
        "request = POST",
        f"url = {_curl_config_value(str(url))}",
        f"header = {_curl_config_value('Content-Type: application/json')}",
    ]
    for key, env_name in sorted(spec.header_env.items()):
        value = os.environ.get(env_name)
        if value is None:
            raise ValueError(f"API header environment variable is not set: {env_name}")
        config_lines.append(f"header = {_curl_config_value(f'{key}: {value}')}")
    config_lines.append(f"data = {_curl_config_value(payload)}")
    config = ("\n".join(config_lines) + "\n").encode()
    return ExecutionInvocation(["curl", "--config", "-"], config)
