"""app.vm_ops: the runner's management-network ssh common part for the VM testbed (spec-testbed-vm-3tier §6-2,
§6-4, §6-6). New module, so a new suite: the always-open connections, the periodic read, the 66 facts, the db01
archive line hand-off and the drift verdict."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import vm_ops
from app.vm_ops import (
    HOSTS,
    HOSTS_BY_NAME,
    MasterSupervisor,
    VmOps,
    VmOpsError,
    chrony_offset_ms,
    cpu_busy_percent,
    executor_argv,
    master_argv,
    parse_read,
    read_stdin,
)

NOW = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)
CHRONY_OK = "7F7F0101,192.168.127.1,3,1795076000.1,-0.000012345,0.000001,0.00002,-3.1,0.001,0.02,0.0003,0.0001,64.2,Normal"


class Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _read_output(host, *, at: datetime = NOW, units: dict | None = None, sums: dict | None = None,
                 drift: tuple = (), guards: tuple = (0, 0), chrony: str = CHRONY_OK,
                 procstat: str = "cpu  100 0 100 800 0 0 0 0 0 0", files: dict | None = None,
                 hostname: str | None = None, end: bool = True) -> str:
    lines = [f"@@ at {at.strftime('%Y-%m-%dT%H:%M:%SZ')}", f"@@ host {hostname or host.name}"]
    for unit in host.units:
        lines.append(f"@@ unit {unit} {(units or {}).get(unit, 'active')}")
    default_sums = {"/var/lib/petition-deploy/war.sha256": "rc=0" if host.role == "was" else "missing",
                    "/var/lib/petition-deploy/expected.sha256": "rc=0"}
    for record, verdict in {**default_sums, **(sums or {})}.items():
        lines.append(f"@@ sums {record} {verdict}")
    lines += [f"@@ drift {path}" for path in drift]
    lines.append(f"@@ guards {guards[0]} {guards[1]}")
    lines.append(f"@@ chrony {chrony}")
    lines.append(f"@@ procstat {procstat}")
    for label, _ in host.files:
        content = (files or {}).get(label)
        payload = "-" if content is None else base64.b64encode(content.encode()).decode()
        lines.append(f"@@ file {label} {payload}")
    if end:
        lines.append("@@ end")
    return "\n".join(lines) + "\n"


class FakeRemote:
    """Answers executor-form ssh calls by management address and the 66 helper call."""

    def __init__(self) -> None:
        self.outputs: dict[str, str | Exception] = {host.address: _read_output(host) for host in HOSTS}
        self.hypervisor: dict | Exception = _hypervisor()
        self.calls: list[tuple[list[str], bytes | None]] = []
        self.masters_down: set[str] = set()

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs.get("input")))
        if "-O" in argv:  # master check: up unless the target is listed as down
            return subprocess.CompletedProcess(argv, 255 if argv[-1].split("@", 1)[1] in self.masters_down else 0, "", "")
        if argv[-1].startswith("hvjob@"):
            if isinstance(self.hypervisor, Exception):
                return subprocess.CompletedProcess(argv, 3, "", str(self.hypervisor))
            verb = kwargs["input"].strip()
            hv = self.hypervisor
            answer = {
                "tunnel": {key: hv[key] for key in ("at", "wg_rx_bytes", "wg_tx_bytes") if key in hv}
                # The helper's real shape: -1 when there was never a handshake (t4-hv-exec-root, review M1).
                | {"handshake_age_s": -1 if hv.get("handshake_age_sec") is None else hv["handshake_age_sec"],
                   "ping": {"sent": hv["ping"]["sent"], "lost": hv["ping"]["sent"] - hv["ping"]["received"],
                            "max_ms": hv["ping"]["max_ms"]}},
                "guards": {"armed": hv.get("guards_active", 0), "jobs": []},
                "version": {"files": hv.get("helper_sha256", {})} if "helper_sha256" in hv else {},
            }[verb]
            return subprocess.CompletedProcess(argv, 0, json.dumps(answer) + "\n", "")
        target = next(item for item in argv if item.startswith("opsjob@"))
        output = self.outputs[target.split("@", 1)[1]]
        if isinstance(output, Exception):
            return subprocess.CompletedProcess(argv, 255, b"", str(output).encode())
        return subprocess.CompletedProcess(argv, 0, output.encode(), b"")


def _hypervisor(at: datetime = NOW, *, rx: int = 0, tx: int = 0, sha: dict | None = None, guards: int = 0) -> dict:
    return {
        "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "handshake_age_sec": 12,
        "ping": {"sent": 10, "received": 10, "max_ms": 1.4},
        "wg_rx_bytes": rx,
        "wg_tx_bytes": tx,
        "guards_active": guards,
        "helper_sha256": sha or dict(HELPER_SHA),
    }


HELPER_SOURCES = {"t4-hv-exec": b"#!/bin/bash\n# helper\n", "t4-hv-exec-root": b"#!/bin/bash\n# root helper\n"}
HELPER_SHA = {name: __import__("hashlib").sha256(body).hexdigest() for name, body in HELPER_SOURCES.items()}


@pytest.fixture(autouse=True)
def _helper_checksums(tmp_path, monkeypatch):
    """The repository's expected checksums file (T4S: civil-petition/deploy/hypervisor/t4-hv-exec.sha256.json)."""
    source = tmp_path / "deploy" / "hypervisor"
    source.mkdir(parents=True)
    path = source / "t4-hv-exec.sha256.json"
    path.write_text(json.dumps({"files": HELPER_SHA}, indent=2))
    monkeypatch.setattr(vm_ops, "HELPER_CHECKSUMS", path)
    return path


def _ops(tmp_path: Path, remote: FakeRemote, clock: Clock | None = None, **kwargs) -> VmOps:
    clock = clock or Clock()
    return VmOps(
        process_runner=remote,
        clock=clock,
        state_dir=tmp_path / "vm-ops",
        archive_state_dir=tmp_path / "petition-archive",
        hypervisor_target="hvjob@192.168.122.1",
        supervisor=MasterSupervisor(process_runner=remote, clock=clock, state_dir=tmp_path / "vm-ops",
                                    control_dir=tmp_path / "cm", popen=lambda *a, **k: None),
        **kwargs,
    )


def test_executor_form_is_the_shared_contract() -> None:
    """The executors (testbed-services T4S) build the same argv: the always-open socket when it is there, an own
    connection when it is not (ControlMaster=no), the executor account, and nothing but `sudo -n bash -s`."""
    argv = executor_argv(HOSTS_BY_NAME["db01"])
    assert argv[0] == "/usr/bin/ssh"
    assert argv[-5:] == ["opsjob@192.168.127.41", "sudo", "-n", "bash", "-s"]
    # T4S contract 3: the same common options on both sides.
    common = [
        "-F", "none", "-i", "/root/.ssh/vm-ops/t4-executor", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=/root/.ssh/vm-ops/known_hosts_civil-petition",
        "-o", "ConnectTimeout=10", "-o", "ControlPath=/app/state/ssh-control/%r@%h:%p",
    ]
    assert argv[1:1 + len(common)] == common
    assert argv[1 + len(common):-5] == ["-o", "ControlMaster=no"]
    master = master_argv(HOSTS_BY_NAME["db01"])
    assert master[1:1 + len(common)] == common
    assert master[1 + len(common):] == [
        "-o", "ControlMaster=yes", "-o", "ControlPersist=yes", "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3", "-N", "-f", "opsjob@192.168.127.41",
    ]
    hypervisor = vm_ops.hypervisor_host("hvjob@192.168.122.1")
    assert master_argv(hypervisor)[-1] == "hvjob@192.168.122.1" and "/root/.ssh/vm-ops/t4-hv" in master_argv(hypervisor)
    # Every VM is reached on its management address only (§3-5 rule 4), the router on its management NIC.
    assert {host.name: host.address for host in HOSTS} == {
        "rt-core01": "192.168.127.2", "web01": "192.168.127.21", "was01": "192.168.127.31",
        "was02": "192.168.127.32", "db01": "192.168.127.41", "loadgen": "192.168.127.10",
    }


def test_control_master_no_falls_back_to_an_own_connection_when_the_socket_is_gone(tmp_path: Path) -> None:
    """Real OpenSSH: with ControlMaster=no and no socket, ssh goes on to connect by itself (so a cleanup reaches
    the VM even with the master dead) instead of failing on the missing socket."""
    assert shutil.which("ssh") is not None, "the runner image and 104 both carry an OpenSSH client"
    result = subprocess.run(
        ["ssh", "-v", "-o", "BatchMode=yes", "-o", "ControlMaster=no", "-o", f"ControlPath={tmp_path}/none",
         "-o", "ConnectTimeout=2", "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={tmp_path}/kh",
         "-p", "1", "nobody@127.0.0.1", "true"],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode != 0
    assert "Connecting to 127.0.0.1" in result.stderr, result.stderr[-500:]


def test_read_body_runs_under_bash_and_parses(tmp_path: Path) -> None:
    """The read body is plain bash: run it for real with stand-ins for the system tools and parse the answer."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stubs = {
        "hostname": "echo db01",
        "systemctl": 'if [[ "$1" == is-active ]]; then echo active; else :; fi',
        "chronyc": f"echo '{CHRONY_OK}'",
    }
    for name, body in stubs.items():
        path = bin_dir / name
        path.write_text(f"#!/usr/bin/env bash\n{body}\n")
        path.chmod(0o755)
    facts_file = tmp_path / "last.json"
    facts_file.write_text('{"host": "db01", "at": "2026-11-20T05:35:00Z"}\n')
    host = vm_ops.VmHost("db01", "x", "db", ("postgresql@16-main.service",), (("archive_state", str(facts_file)),))
    result = subprocess.run(
        ["bash", "-s"], input=read_stdin(host), capture_output=True, timeout=20,
        env={"PATH": f"{bin_dir}:{os.environ['PATH']}", "LC_ALL": "C"},
    )
    assert result.returncode == 0, result.stderr
    facts = parse_read(host, result.stdout.decode())
    assert facts.units == {"postgresql@16-main.service": "active"}
    assert facts.chrony == CHRONY_OK
    assert len(facts.procstat) >= 8
    assert json.loads(facts.files["archive_state"])["host"] == "db01"
    # The sudo command line carries nothing: the arguments ride on stdin's `set --` line.
    assert read_stdin(host).startswith(b"set -- postgresql@16-main.service -- archive_state=")


def test_parse_rejects_a_cut_read_or_the_wrong_vm() -> None:
    host = HOSTS_BY_NAME["web01"]
    with pytest.raises(VmOpsError, match="did not finish"):
        parse_read(host, _read_output(host, end=False))
    with pytest.raises(VmOpsError, match="answered as 'was01'"):
        parse_read(host, _read_output(host, hostname="was01"))


def test_clock_and_cpu_arithmetic() -> None:
    assert chrony_offset_ms(CHRONY_OK) == pytest.approx(0.012345)
    with pytest.raises(VmOpsError, match="not synchronised"):
        chrony_offset_ms(CHRONY_OK.replace("Normal", "Not synchronised"))
    # 8 fields count; guest and guest_nice (already inside user and nice) do not.
    older = [100, 0, 100, 800, 0, 0, 0, 0, 0, 0]
    newer = [400, 0, 200, 1100, 100, 0, 0, 0, 999, 0]
    assert cpu_busy_percent(older, newer) == pytest.approx(100 * 400 / 800)
    with pytest.raises(VmOpsError, match="backwards"):
        cpu_busy_percent(newer, older)


def test_supervisor_opens_dead_masters_records_the_time_and_reopens_after_a_drop(tmp_path: Path) -> None:
    clock = Clock()
    alive: set[str] = set()
    opened: list[str] = []

    class Process:
        def __init__(self, address: str) -> None:
            self.address = address
            self.terminated = False

        def poll(self):
            return None if self.address in alive else 255

        def terminate(self):
            self.terminated = True
            alive.discard(self.address)

        def wait(self, timeout=None):
            return 0

    processes: list[Process] = []

    def popen(argv, **kwargs):
        address = argv[-1].split("@", 1)[1]
        opened.append(address)
        alive.add(address)
        process = Process(address)
        processes.append(process)
        return process

    exits: list[str] = []

    def check(argv, **kwargs):
        address = argv[-1].split("@", 1)[1]
        if argv[-2:-1] == ["exit"]:
            exits.append(address)
            alive.discard(address)
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0 if address in alive else 255, "", "")

    supervisor = MasterSupervisor(popen=popen, process_runner=check, clock=clock, state_dir=tmp_path / "s",
                                  control_dir=tmp_path / "cm", sleep=lambda _: None)
    status = supervisor.ensure()
    assert sorted(opened) == sorted(host.address for host in HOSTS)
    assert all(item["alive"] and item["opened_at"] == "2026-11-20T06:00:00Z" for item in status.values())
    assert oct((tmp_path / "cm").stat().st_mode & 0o777) == "0o700"
    assert json.loads((tmp_path / "s" / "masters.json").read_text())["db01"]["alive"] is True

    clock.value += timedelta(minutes=40)
    supervisor.ensure()  # all alive: nothing reopened, opened_at kept
    assert len(opened) == 6 and supervisor.snapshot()["db01"]["opened_at"] == "2026-11-20T06:00:00Z"

    alive.discard("192.168.127.41")  # the db01 connection dropped
    supervisor.ensure()
    assert opened[-1] == "192.168.127.41" and len(opened) == 7
    assert supervisor.snapshot()["db01"]["opened_at"] == "2026-11-20T06:40:00Z"
    assert supervisor.snapshot()["web01"]["opened_at"] == "2026-11-20T06:00:00Z"

    supervisor.stop()  # -O exit on every socket (with -f the master is not the runner's child), no pattern kill
    assert alive == set() and sorted(exits) == sorted(host.address for host in HOSTS)
    assert all(not item["alive"] for item in supervisor.snapshot().values())
    assert all(not item["alive"] for item in supervisor.snapshot().values())


def test_supervisor_reports_why_a_master_did_not_open(tmp_path: Path) -> None:
    class Dead:
        def poll(self):
            return 255  # -f exits 0 only after the master forked

        def terminate(self):
            pass

    def popen(argv, stderr=None, **kwargs):
        stderr.write("opsjob@192.168.127.2: Permission denied (publickey).\n")
        return Dead()

    supervisor = MasterSupervisor(
        hosts=[HOSTS_BY_NAME["rt-core01"]], popen=popen,
        process_runner=lambda argv, **k: subprocess.CompletedProcess(argv, 255, "", ""),
        clock=Clock(), state_dir=tmp_path / "s", control_dir=tmp_path / "cm", sleep=lambda _: None,
    )
    status = supervisor.ensure()["rt-core01"]
    assert status["alive"] is False and status["opened_at"] is None
    assert "Permission denied (publickey)" in status["detail"]


def test_periodic_read_records_every_vm_and_66_and_hands_db01_line_over_unchanged(tmp_path: Path) -> None:
    remote = FakeRemote()
    line = '{"host": "db01", "at": "2026-11-20T05:35:02Z", "db": {"rows": {"petition": 2080000}}}'
    remote.outputs["192.168.127.41"] = _read_output(HOSTS_BY_NAME["db01"], files={"archive_state": line})
    remote.outputs["192.168.127.21"] = RuntimeError("ssh: connect to host 192.168.127.21 port 22: No route to host")
    ops = _ops(tmp_path, remote)
    document = ops.read_all()
    # One VM down does not stop the others; its error is kept word for word.
    assert document["hosts"]["web01"]["last_error"]["detail"].endswith("No route to host")
    assert document["hosts"]["db01"]["facts"]["units"]["postgresql@16-main.service"] == "active"
    assert document["hosts"]["rt-core01"]["cpu_history"][0]["procstat"][:4] == [100, 0, 100, 800]
    assert (tmp_path / "petition-archive" / "last.json").read_text() == line + "\n"
    assert document["hypervisor"]["history"][-1]["handshake_age_sec"] == 12
    # Every VM call is the executor form over the socket with `sudo -n bash -s` and the body on stdin.
    vm_calls = [(argv, stdin) for argv, stdin in remote.calls
                if any(a.startswith("opsjob@") for a in argv) and "-O" not in argv]
    assert len(vm_calls) == 6
    assert all(argv[-4:] == ["sudo", "-n", "bash", "-s"] and stdin.startswith(b"set -- ") for argv, stdin in vm_calls)
    assert all("ControlMaster=no" in argv for argv, _ in vm_calls)
    # Nothing the read sends names a scenario, the testbed or the injection tooling (principle 8).
    for _, stdin in vm_calls:
        text = stdin.decode().lower()
        for word in ("scenario", "testbed", "t4", "inject"):
            assert word not in text, word

    # A db01 line that is not JSON does not replace the good one.
    remote.outputs["192.168.127.41"] = _read_output(HOSTS_BY_NAME["db01"], files={"archive_state": "{broken"})
    ops.read_all()
    assert (tmp_path / "petition-archive" / "last.json").read_text() == line + "\n"


def test_hypervisor_history_is_capped_and_its_errors_are_recorded(tmp_path: Path) -> None:
    remote = FakeRemote()
    clock = Clock()
    ops = _ops(tmp_path, remote, clock)
    for index in range(vm_ops.HYPERVISOR_HISTORY + 3):
        remote.hypervisor = _hypervisor(clock.value, rx=index)
        ops.hypervisor_facts()
        clock.value += timedelta(minutes=5)
    history = ops.load()["hypervisor"]["history"]
    assert len(history) == vm_ops.HYPERVISOR_HISTORY and history[-1]["wg_rx_bytes"] == vm_ops.HYPERVISOR_HISTORY + 2
    remote.hypervisor = RuntimeError("unknown request")
    document = ops.read_all()
    assert "hypervisor: tunnel refused: unknown request" in document["hypervisor"]["last_error"]["detail"]
    remote.hypervisor = {k: v for k, v in _hypervisor().items() if k != "helper_sha256"}
    with pytest.raises(VmOpsError, match="lacks 'files'"):
        ops.hypervisor_facts()
    # The helper takes its request on stdin's first line; the ssh command line names nothing.
    argv, stdin = remote.calls[-1]
    assert stdin == "version\n" and argv[-1] == "hvjob@192.168.122.1" and "ControlMaster=no" in argv


def test_drift_detail_names_what_differs_and_leaves_stale_reads_to_preflight(tmp_path: Path) -> None:
    remote = FakeRemote()
    clock = Clock()
    ops = _ops(tmp_path, remote, clock)
    ops.read_all()
    assert ops.drift_detail() is None

    remote.outputs["192.168.127.31"] = _read_output(
        HOSTS_BY_NAME["was01"], units={"tomcat10.service": "inactive"},
        sums={"/var/lib/petition-deploy/war.sha256": "rc=1"}, drift=("petition.war",),
    )
    remote.outputs["192.168.127.2"] = _read_output(
        HOSTS_BY_NAME["rt-core01"], sums={"/var/lib/petition-deploy/expected.sha256": "missing"},
    )
    remote.hypervisor = _hypervisor(sha={**HELPER_SHA, "t4-hv-exec": "c" * 64})
    ops.read_all()
    detail = ops.drift_detail()
    assert "was01 tomcat10.service inactive" in detail
    assert "was01 /var/lib/petition-deploy/war.sha256 rc=1" in detail
    assert "was01 changed since install: petition.war" in detail
    assert "rt-core01 /var/lib/petition-deploy/expected.sha256 missing" in detail
    assert f"hypervisor t4-hv-exec installed cccccccccccc != repository {HELPER_SHA['t4-hv-exec'][:12]}" in detail
    # A WAR record is required only on the WAS pair (web01 has none and passes).
    assert "web01" not in detail

    # The expected file is read as JSON (spacing does not matter); a file without an entry stops with why.
    path = vm_ops.HELPER_CHECKSUMS
    path.write_text(json.dumps({"files": {"t4-hv-exec": HELPER_SHA["t4-hv-exec"]}}))
    with pytest.raises(VmOpsError, match="lack \\['t4-hv-exec-root'\\]"):
        ops.drift_detail()
    path.write_text(json.dumps({"files": HELPER_SHA}, separators=(",", ":")))
    remote.hypervisor = _hypervisor()
    remote.outputs = FakeRemote().outputs
    ops.read_all()
    assert ops.drift_detail() is None

    # Stale reads say nothing here (preflight refuses them with the read error).
    clock.value += timedelta(seconds=vm_ops.FACTS_MAX_AGE_SEC + 1)
    assert ops.drift_detail() is None


def test_get_vm_ops_is_off_unless_enabled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(vm_ops, "_ops", None)
    monkeypatch.setattr(vm_ops, "_service", None)
    monkeypatch.delenv("CIVIL_PETITION_OPS", raising=False)
    assert vm_ops.get_vm_ops() is None and vm_ops.get_vm_ops_service() is None
    monkeypatch.setenv("CIVIL_PETITION_OPS", "on")
    monkeypatch.setenv("PETITION_ARCHIVE_STATE_DIR", str(tmp_path / "archive"))
    ops = vm_ops.get_vm_ops()
    assert ops is not None and ops.archive_state_dir == tmp_path / "archive"
    # 66 gets a master too (T4S contract 6), with its own account and key.
    hypervisor = ops.supervisor.hosts[-1]
    assert [host.name for host in ops.supervisor.hosts] == [*(host.name for host in HOSTS), "pve66"]
    assert (hypervisor.user, hypervisor.address, hypervisor.key.name) == ("hvjob", "192.168.122.1", "t4-hv")
    assert vm_ops.get_vm_ops_service().ops is ops


@pytest.mark.asyncio
async def test_service_checks_masters_reads_periodically_and_stops_its_masters(tmp_path: Path) -> None:
    events: list[str] = []

    class Supervisor:
        def ensure(self):
            events.append("check")

        def stop(self):
            events.append("stop")

    class Ops:
        supervisor = Supervisor()

        def read_all(self):
            events.append("read")

    ticks = iter([0.0, 0.0, 10.0, 10.0, 400.0, 400.0, 400.0, 400.0] + [400.0] * 50)
    service = vm_ops.VmOpsService(Ops(), check_interval_sec=0.01, read_interval_sec=300, monotonic=lambda: next(ticks))
    service.ensure_started()
    for _ in range(100):
        if events.count("read") >= 2:
            break
        await asyncio.sleep(0.01)
    await service.stop()
    assert events[0] == "check" and events.count("read") >= 2 and events[-1] == "stop"
    assert events.count("check") >= 3


class _Opened:
    """The first `ssh -M -N -f` process: exits with `code` (0 = it forked), or keeps running (None)."""

    def __init__(self, code):
        self.code = code
        self.terminated = False

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated = True


def test_a_dead_socket_file_is_removed_before_the_master_is_reopened(tmp_path: Path) -> None:
    """Review rv-t4r3 H1: a master killed without cleanup leaves its socket file on the state volume, and
    ControlMaster=yes then cannot listen there and goes on as a plain detached connection. The supervisor removes
    the dead file before it opens, so the new master takes the socket."""
    host = HOSTS_BY_NAME["db01"]
    control = tmp_path / "cm"
    control.mkdir()
    socket = control / "opsjob@192.168.127.41:22"
    socket.write_text("")  # what a SIGKILLed master leaves (a dead unix socket entry)
    seen_at_open = []
    alive: set[str] = set()

    def popen(argv, **kwargs):
        seen_at_open.append(socket.exists())
        alive.add(argv[-1])
        return _Opened(0)

    def check(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0 if argv[-1] in alive else 255, "", "")

    supervisor = MasterSupervisor(hosts=[host], popen=popen, process_runner=check, clock=Clock(),
                                  state_dir=tmp_path / "s", control_dir=control, sleep=lambda _: None)
    assert supervisor.socket_path(host) == socket
    status = supervisor.ensure()["db01"]
    assert seen_at_open == [False] and status["alive"] is True


def test_an_open_that_leaves_no_master_is_not_retried_and_logins_do_not_pile_up(tmp_path: Path) -> None:
    """Review rv-t4r3 H1: when the opening ssh forked (exit 0) but no master answers, a plain connection may be
    left on the VM that the runner cannot see or end. Trying again every check would add a login and a new
    host_connections row each time, so the supervisor gives up on that host until the runner restarts and the
    status (preflight: mgmt_ssh_down) says why."""
    clock = Clock()
    opens: list[str] = []

    def popen(argv, stderr=None, **kwargs):
        opens.append(argv[-1])
        stderr.write("ControlSocket /app/state/ssh-control/opsjob@192.168.127.2:22 already exists, disabling multiplexing\n")
        return _Opened(0)

    supervisor = MasterSupervisor(
        hosts=[HOSTS_BY_NAME["rt-core01"]], popen=popen,
        process_runner=lambda argv, **k: subprocess.CompletedProcess(argv, 255, "", ""),
        clock=clock, state_dir=tmp_path / "s", control_dir=tmp_path / "cm", sleep=lambda _: None, open_wait_sec=3,
    )
    for _ in range(40):  # 40 checks over 20 minutes
        status = supervisor.ensure()["rt-core01"]
        clock.value += timedelta(seconds=30)
    assert opens == ["opsjob@192.168.127.2"]
    assert status["alive"] is False and status["given_up"] is True
    assert "disabling multiplexing" in status["detail"] and "until it restarts" in status["detail"]


def test_an_open_that_failed_by_itself_is_retried_after_the_backoff_only(tmp_path: Path) -> None:
    """A VM that is rebooting refuses the connection (ssh exits 255, nothing is left on the VM): try again, but
    not on every check."""
    clock = Clock()
    opens: list[str] = []
    hung = _Opened(None)
    answers = [_Opened(255), hung]

    def popen(argv, stderr=None, **kwargs):
        opens.append(argv[-1])
        stderr.write("ssh: connect to host 192.168.127.2 port 22: Connection refused\n")
        return answers.pop(0) if answers else _Opened(255)

    supervisor = MasterSupervisor(
        hosts=[HOSTS_BY_NAME["rt-core01"]], popen=popen,
        process_runner=lambda argv, **k: subprocess.CompletedProcess(argv, 255, "", ""),
        clock=clock, state_dir=tmp_path / "s", control_dir=tmp_path / "cm", sleep=lambda _: None, open_wait_sec=3,
    )
    status = supervisor.ensure()["rt-core01"]
    assert len(opens) == 1 and "Connection refused" in status["detail"] and not status.get("given_up")
    for _ in range(9):  # 4.5 minutes of checks: no new attempt
        clock.value += timedelta(seconds=30)
        supervisor.ensure()
    assert len(opens) == 1
    clock.value += timedelta(seconds=31)  # past MASTER_RETRY_SEC
    supervisor.ensure()
    assert len(opens) == 2
    # The second open hung before forking: the runner ends its own process and it counts as a clean failure.
    assert hung.terminated is True and supervisor.snapshot()["rt-core01"].get("retry_at")


def test_the_66_target_is_fixed_to_the_executors_value() -> None:
    """Review rv-t4r3 L2: the socket name comes from the target, and the executors (testbed-services
    vm_executor_common, T4S) call hvjob@192.168.122.1. A runner-only setting would split the sockets and refuse every
    66 executor run, so the value is fixed in code, not read from the environment."""
    import inspect

    assert vm_ops.HYPERVISOR_TARGET == "hvjob@192.168.122.1"
    assert "VM_OPS_HYPERVISOR_TARGET" not in inspect.getsource(vm_ops)
    host = vm_ops.hypervisor_host()
    assert (host.user, host.address) == ("hvjob", "192.168.122.1")


def test_runner_reads_never_log_in_directly_while_a_master_is_down(tmp_path: Path) -> None:
    """Review rv-t4r3 R-M1: with ControlMaster=no a read would log in by itself when the master is gone (given up
    or waiting for its retry), a new login and host_connections row every 5 minutes (Q7c). The runner's reads
    check the master first and fail instead; the facts go stale and preflight stops the domain."""
    remote = FakeRemote()
    remote.masters_down = {host.address for host in HOSTS} | {"192.168.122.1"}
    ops = _ops(tmp_path, remote)
    document = ops.read_all()
    assert [argv for argv, _ in remote.calls if "-O" not in argv] == []
    for host in HOSTS:
        assert f"control connection to {host.address} is down" in document["hosts"][host.name]["last_error"]["detail"]
        assert document["hosts"][host.name].get("facts") is None
    assert "control connection to 192.168.122.1 is down" in document["hypervisor"]["last_error"]["detail"]
    with pytest.raises(VmOpsError, match="control connection to 192.168.127.10 is down"):
        ops.read_loadgen_live()
    with pytest.raises(VmOpsError, match="control connection to 192.168.122.1 is down"):
        ops.hypervisor_facts()
    assert [argv for argv, _ in remote.calls if "-O" not in argv] == []
    # One master back: only that host is read.
    remote.masters_down.discard("192.168.127.41")
    ops.read_all()
    assert [a for argv, _ in remote.calls if "-O" not in argv for a in argv if a.startswith("opsjob@")] == [
        "opsjob@192.168.127.41"]


def test_a_check_that_times_out_does_not_remove_or_reopen_a_socket(tmp_path: Path) -> None:
    """Review rv-t4r3 R-L1: a check that timed out says nothing about the master, so its socket stays."""
    control = tmp_path / "cm"
    control.mkdir()
    socket = control / "opsjob@192.168.127.2:22"
    socket.write_text("")
    opens = []

    def check(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 10)

    supervisor = MasterSupervisor(hosts=[HOSTS_BY_NAME["rt-core01"]], popen=lambda *a, **k: opens.append(a),
                                  process_runner=check, clock=Clock(), state_dir=tmp_path / "s", control_dir=control,
                                  sleep=lambda _: None)
    supervisor.ensure()
    assert opens == [] and socket.exists()


# --- Safety rules the review mutants showed unpinned (rv-t4r2 M3, L5) --------------------------------------------

def test_a_live_master_this_process_did_not_open_starts_the_settle_now(tmp_path: Path) -> None:
    """rv-t4r2 R19: a socket that is alive but was not opened by this runner process (left by an earlier runner
    process in the same container) has an unknown open time, so the 30-minute settle counts from now, never from
    an older time that would let a recording start right away."""
    clock = Clock()
    opens = []
    supervisor = MasterSupervisor(
        hosts=[HOSTS_BY_NAME["db01"]], popen=lambda *a, **k: opens.append(a),
        process_runner=lambda argv, **k: subprocess.CompletedProcess(argv, 0, "", ""),
        clock=clock, state_dir=tmp_path / "s", control_dir=tmp_path / "cm", sleep=lambda _: None,
    )
    status = supervisor.ensure()["db01"]
    assert opens == []
    assert status == {"alive": True, "opened_at": "2026-11-20T06:00:00Z", "detail": "found open"}
    clock.value += timedelta(minutes=10)
    assert supervisor.ensure()["db01"]["opened_at"] == "2026-11-20T06:00:00Z"  # kept, not moved later or earlier


def test_stale_reads_with_drift_stay_out_of_the_drift_reason(tmp_path: Path) -> None:
    """rv-t4r2 R24: the drift gate judges only fresh reads. A stale read that once showed drift does not name it
    (it might be fixed since); preflight refuses the stale facts instead (see test_preflight_probe)."""
    remote = FakeRemote()
    clock = Clock()
    ops = _ops(tmp_path, remote, clock)
    remote.outputs["192.168.127.31"] = _read_output(HOSTS_BY_NAME["was01"], drift=("petition.war",),
                                                    sums={"/var/lib/petition-deploy/war.sha256": "rc=1"})
    ops.read_all()
    assert "was01 changed since install: petition.war" in ops.drift_detail()
    clock.value += timedelta(seconds=vm_ops.FACTS_MAX_AGE_SEC + 1)
    remote.hypervisor = _hypervisor(clock.value)
    ops.hypervisor_facts()  # 66 fresh, VMs stale
    assert ops.drift_detail() is None


def test_only_the_db01_line_reaches_the_archive_state(tmp_path: Path) -> None:
    """rv-t4r2 R13: an archive-state file carried by any other VM's read is never delivered as db01's line."""
    remote = FakeRemote()
    ops = _ops(tmp_path, remote)
    forged = '{"host": "db01", "at": "2026-11-20T05:35:02Z"}'
    remote.outputs["192.168.127.10"] = _read_output(HOSTS_BY_NAME["loadgen"]).replace(
        "@@ end", f"@@ file archive_state {base64.b64encode(forged.encode()).decode()}\n@@ end")
    ops.read_all()
    assert not (tmp_path / "petition-archive" / "last.json").exists()
