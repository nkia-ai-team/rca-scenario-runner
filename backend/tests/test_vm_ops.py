"""app.vm_ops: the runner's management-network ssh common part for the VM testbed (spec-testbed-vm-3tier §6-2,
§6-4, §6-6). New module, so a new suite: the always-open connections, the periodic read, the 66 facts, the db01
archive line hand-off and the drift verdict."""
from __future__ import annotations

import asyncio
import base64
import contextlib
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
    # A plain path is named by its record the way the read body names it: absolute paths come from
    # expected.sha256, the WAR (relative to webapps) from war.sha256; a (record, path) pair names it directly.
    lines += [
        f"@@ drift {item[0]} {item[1]}" if isinstance(item, tuple) else
        f"@@ drift /var/lib/petition-deploy/{'expected' if item.startswith('/') else 'war'}.sha256 {item}"
        for item in drift
    ]
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

    class Routine:
        def tick(self):
            events.append("routine")

    class Ops:
        supervisor = Supervisor()
        routine = Routine()

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
    # The routine operator session is asked every round (Q35 condition ②: it runs at all times).
    assert events.count("routine") >= events.count("read")
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



# --- Routine operator session (Q35 answer condition ②, T4O contract 4, 6) -----------------------------------------

class _Draws:
    """A fixed random source: uniform returns the low end plus `frac` of the span; choice walks the list."""

    def __init__(self, frac: float = 0.5) -> None:
        self.frac = frac
        self.index = 0

    def uniform(self, low, high):
        return low + (high - low) * self.frac

    def choice(self, items):
        item = items[self.index % len(items)]
        self.index += 1
        return item


def _routine(tmp_path: Path, codes: list, *, clock: Clock | None = None, rng=None):
    clock = clock or Clock()
    calls = []

    def run(argv, **kwargs):
        if argv[-1] == "--list-nodes":  # the CLI's own list (review rv-t4o L3)
            return subprocess.CompletedProcess(argv, 0, json.dumps(list(vm_ops.OPERATOR_ROUTINE_HOSTS)) + "\n", "")
        calls.append((argv, kwargs))
        code = codes.pop(0) if codes else 0
        return subprocess.CompletedProcess(argv, code, "", "" if code == 0 else "ssh: connect to host 192.168.127.21 port 22: timed out\n")

    routine = vm_ops.OperatorRoutine(process_runner=run, clock=clock, state_dir=tmp_path / "s", rng=rng or _Draws(),
                                     cli=Path("/opt/lucida/scenario-contracts/profiles/operator_session.py"))
    return routine, calls, clock


def test_routine_operator_session_runs_on_its_schedule_and_records_the_outcome(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VM_OPS_KEY_DIR", "/root/.ssh/vm-ops")
    monkeypatch.setenv("CIVIL_PETITION_SSH_CONTROL_DIR", "/app/state/ssh-control")
    routine, calls, clock = _routine(tmp_path, [0, 75, 0, 2])
    # No record: the first session is planned 1 to 10 minutes out, nothing runs yet.
    document = routine.tick()
    assert calls == [] and document["next"] == {"at": "2026-11-20T06:05:30Z", "host": "rt-core01"}
    assert routine.last_ok_age_sec(clock.value) == vm_ops.OPERATOR_ROUTINE_MAX_AGE_SEC + 1
    clock.value += timedelta(minutes=5)
    routine.tick()
    assert calls == []
    clock.value += timedelta(seconds=31)
    document = routine.tick()  # due: one session through the testbed-services CLI, read-only check
    argv, kwargs = calls[0]
    assert argv == ["python3", "/opt/lucida/scenario-contracts/profiles/operator_session.py", "--node", "rt-core01", "--check"]
    assert kwargs["env"]["VM_OPS_KEY_DIR"] == "/root/.ssh/vm-ops"
    assert kwargs["env"]["CIVIL_PETITION_SSH_CONTROL_DIR"] == "/app/state/ssh-control"
    assert document["last_ok"] == {"at": "2026-11-20T06:05:31Z", "host": "rt-core01"}
    # The next one 30 to 90 minutes later (here the middle, 60), on the next drawn VM.
    assert document["next"] == {"at": "2026-11-20T07:05:31Z", "host": "web01"}
    clock.value = datetime(2026, 11, 20, 7, 5, 31, tzinfo=timezone.utc)
    document = routine.tick()  # 75: an executor's operator session holds web01, the same VM 5 minutes later
    assert len(calls) == 2 and document["next"] == {"at": "2026-11-20T07:10:31Z", "host": "web01"}
    assert document["last_ok"]["at"] == "2026-11-20T06:05:31Z" and document["last"]["rc"] == 75
    clock.value += timedelta(minutes=5)
    document = routine.tick()
    assert calls[-1][0][3] == "web01" and document["last_ok"] == {"at": "2026-11-20T07:10:31Z", "host": "web01"}
    clock.value = vm_ops._parse(document["next"]["at"])
    document = routine.tick()  # a failure is recorded with its words; the schedule goes on, last_ok stays
    assert document["last"]["rc"] == 2 and "timed out" in document["last"]["detail"]
    assert document["last_ok"]["host"] == "web01" and vm_ops._parse(document["next"]["at"]) > clock.value
    # The record survives a runner restart (a new scheduler on the same state).
    again, _, _ = _routine(tmp_path, [], clock=clock)
    assert again.load()["last_ok"] == document["last_ok"]


def test_routine_operator_sessions_cover_the_router_and_servers_at_30_to_90_minutes(tmp_path: Path) -> None:
    import random

    clock = Clock()
    routine, calls, _ = _routine(tmp_path, [], clock=clock, rng=random.Random(7))
    routine.tick()
    starts = []
    for _ in range(2000):
        clock.value += timedelta(minutes=1)
        before = len(calls)
        routine.tick()
        if len(calls) > before:
            starts.append(clock.value)
    hosts = {argv[3] for argv, _ in calls}
    assert hosts == set(vm_ops.OPERATOR_ROUTINE_HOSTS) and "loadgen" not in hosts
    gaps = [(b - a).total_seconds() for a, b in zip(starts, starts[1:])]
    assert gaps and min(gaps) >= 1800 and max(gaps) <= 5400 + 60
    assert len(starts) >= 2000 // 90


def test_routine_intervals_are_spread_over_the_whole_30_to_90_minutes(tmp_path: Path) -> None:
    """Review rv-t4o M4 (mutant R4: a fixed 60-minute gap passed): 2000 draws reach below 35 and above 85 minutes."""
    import random

    routine, _, clock = _routine(tmp_path, [], rng=random.Random(11))
    gaps = [(vm_ops._parse(routine._draw(clock.value)["at"]) - clock.value).total_seconds() for _ in range(2000)]
    assert min(gaps) >= 1800 and max(gaps) <= 5400
    assert min(gaps) < 35 * 60 and max(gaps) > 85 * 60


def test_a_vm_busy_three_times_in_a_row_hands_the_routine_to_another_vm(tmp_path: Path) -> None:
    """Review rv-t4o M2: a lock held for good (an ssh left behind) must not pin the routine to one VM. After three
    busy answers in a row the next session goes to another VM, and the record says so."""
    routine, calls, clock = _routine(tmp_path, [75, 75, 75])
    routine.tick()  # plans rt-core01
    for attempt in range(3):
        clock.value = vm_ops._parse(routine.load()["next"]["at"])
        document = routine.tick()
    assert [argv[3] for argv, _ in calls] == ["rt-core01"] * 3
    assert document["last"]["switched_from"] == "rt-core01"
    assert "busy 3 times in a row" in document["last"]["detail"]
    assert document["next"]["host"] != "rt-core01" and "busy_count" not in document
    # Two busy answers then a success: no switch, the count starts over.
    (tmp_path / "b").mkdir()
    routine, calls, clock = _routine(tmp_path / "b", [75, 75, 0, 75])
    routine.tick()
    for _ in range(4):
        clock.value = vm_ops._parse(routine.load()["next"]["at"])
        document = routine.tick()
    assert "switched_from" not in document["last"] and document["busy_count"] == 1


def test_the_routine_takes_its_vm_list_from_the_cli_and_stops_without_it(tmp_path: Path) -> None:
    """Review rv-t4o L3: one VM list, the CLI's (`--list-nodes`). Without it no session runs and the record says
    why (preflight then stops the domain once the last success is 180 minutes old)."""
    clock = Clock()
    asked = []

    def run(argv, **kwargs):
        asked.append(argv[2:])
        if argv[-1] == "--list-nodes":
            return subprocess.CompletedProcess(argv, 1, "", "operator_session.py: no inventory\n")
        raise AssertionError(argv)

    routine = vm_ops.OperatorRoutine(process_runner=run, clock=clock, state_dir=tmp_path / "s", rng=_Draws())
    document = routine.tick()
    assert "no inventory" in document["stopped"]["detail"] and "next" not in document
    assert asked == [["--list-nodes"]]

    listed = ["web01", "db01"]

    def run_listed(argv, **kwargs):
        if argv[-1] == "--list-nodes":
            return subprocess.CompletedProcess(argv, 0, json.dumps(listed), "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    routine = vm_ops.OperatorRoutine(process_runner=run_listed, clock=clock, state_dir=tmp_path / "s", rng=_Draws())
    document = routine.tick()
    assert "stopped" not in document and document["next"]["host"] in listed
    # A planned VM the list no longer carries is redrawn from the list, never run.
    listed[:] = ["db01"]
    routine._nodes = None
    document = routine.tick()
    assert document["next"]["host"] == "db01"


def test_run_in_group_ends_the_whole_group_on_timeout(tmp_path: Path) -> None:
    """Review rv-t4o M2: the routine CLI is ended with the ssh it started."""
    import time

    pid_file = tmp_path / "child.pid"
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        vm_ops.run_in_group(["bash", "-c", f"sleep 60 & echo $! > {pid_file}; wait"], timeout=1,
                            env={"PATH": os.environ["PATH"]})
    assert time.monotonic() - started < 20  # the group was ended, the 60-second child did not run out
    child = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
    # T4S: the CLI ends its own ssh group after 90 seconds (exit 124); the runner waits 150.
    assert (vm_ops.OPERATOR_SESSION_CLI_LIMIT_SEC, vm_ops.OPERATOR_ROUTINE_TIMEOUT_SEC) == (90, 150)


@pytest.mark.asyncio
async def test_the_routine_does_not_hold_up_the_master_checks(tmp_path: Path) -> None:
    """Review rv-t4o L4: a routine session that takes long runs in its own loop; master checks go on meanwhile."""
    import threading

    checks: list[float] = []
    release = threading.Event()

    class Supervisor:
        def ensure(self):
            checks.append(1)

        def stop(self):
            pass

    class Routine:
        def tick(self):
            release.wait(5)

    class Ops:
        supervisor = Supervisor()
        routine = Routine()

        def read_all(self):
            pass

    service = vm_ops.VmOpsService(Ops(), check_interval_sec=0.01, read_interval_sec=300)
    service.ensure_started()
    for _ in range(100):
        if len(checks) >= 5:
            break
        await asyncio.sleep(0.01)
    release.set()
    await service.stop()
    assert len(checks) >= 5


def test_run_in_group_does_not_wait_for_a_grandchild_in_another_session(tmp_path: Path) -> None:
    """Review rv-t4o2 N3: the CLI starts its ssh in a session of its own, so ending the CLI's group does not reach
    it, and that ssh still holds the pipes. The runner waits a few seconds for them and then stops reading."""
    import sys
    import time

    pid_file = tmp_path / "grandchild.pid"
    cli = tmp_path / "cli.py"
    cli.write_text(
        "import subprocess, sys, time\n"
        "p = subprocess.Popen(['sleep', '20'], start_new_session=True)\n"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            vm_ops.run_in_group([sys.executable, str(cli)], timeout=1, env={"PATH": os.environ["PATH"]})
        elapsed = time.monotonic() - started
        assert elapsed < 1 + 2 + vm_ops.PIPE_DRAIN_SEC + 1, elapsed
    finally:
        if pid_file.exists():
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(pid_file.read_text()), 9)


def test_kill_process_group_returns_as_soon_as_a_reaped_group_is_gone(tmp_path: Path) -> None:
    """Review rv-t4o2 N4: with the leader passed it is reaped while waiting, so a group that died on SIGTERM does not
    sit out the whole grace."""
    import time

    process = subprocess.Popen(["bash", "-c", "sleep 60 & wait"], start_new_session=True)
    time.sleep(0.2)
    started = time.monotonic()
    vm_ops.kill_process_group(process.pid, leader=process)
    assert time.monotonic() - started < 2
    assert process.returncode is not None


def test_drift_detail_excuses_only_a_held_decoys_files_on_its_node(tmp_path: Path) -> None:
    """Command-session decision D1 (b): the file a VM decoy held through the recording changed is no drift on its
    node (nor the record's rc=1 it causes); another file there, or the same file on another node, still is."""
    remote = FakeRemote()
    clock = Clock()
    ops = _ops(tmp_path, remote, clock)
    site = "/etc/apache2/sites-available/petition.conf"
    remote.outputs["192.168.127.21"] = _read_output(
        HOSTS_BY_NAME["web01"], sums={"/var/lib/petition-deploy/expected.sha256": "rc=1"}, drift=(site,),
    )
    ops.read_all()
    assert "web01 changed since install: " + site in ops.drift_detail()
    assert ops.drift_detail(excused={"web01": {"paths": {site}, "guards": 1}}) is None
    detail = ops.drift_detail(excused={"web02": {"paths": {site}, "guards": 1}})
    assert "web01 changed since install: " + site in detail and "web01 /var/lib/petition-deploy/expected.sha256 rc=1" in detail
    remote.outputs["192.168.127.21"] = _read_output(
        HOSTS_BY_NAME["web01"], sums={"/var/lib/petition-deploy/expected.sha256": "rc=1"},
        drift=(site, "/etc/apache2/apache2.conf"),
    )
    ops.read_all()
    detail = ops.drift_detail(excused={"web01": {"paths": {site}, "guards": 1}})
    assert detail == ("web01 /var/lib/petition-deploy/expected.sha256 rc=1; "
                      "web01 changed since install: /etc/apache2/apache2.conf")
    # A record that cannot be read is never a decoy's doing.
    remote.outputs["192.168.127.21"] = _read_output(
        HOSTS_BY_NAME["web01"], sums={"/var/lib/petition-deploy/expected.sha256": "missing"}, drift=(site,),
    )
    ops.read_all()
    assert "expected.sha256 missing" in ops.drift_detail(excused={"web01": {"paths": {site}, "guards": 1}})


def test_drift_detail_excuses_a_held_war_decoy_on_the_war_record(tmp_path: Path) -> None:
    """war.sha256 names the WAR relative to webapps; the CLI's changed line is absolute. A held war decoy excuses it
    on its own node, not on the other WAS."""
    remote = FakeRemote()
    clock = Clock()
    ops = _ops(tmp_path, remote, clock)
    for name, address in (("was01", "192.168.127.31"), ("was02", "192.168.127.32")):
        remote.outputs[address] = _read_output(
            HOSTS_BY_NAME[name], sums={"/var/lib/petition-deploy/war.sha256": "rc=1"}, drift=("petition.war",),
        )
    ops.read_all()
    detail = ops.drift_detail(excused={"was01": {"paths": {"/var/lib/tomcat10/webapps/petition.war"}, "guards": 1}})
    assert detail == ("was02 /var/lib/petition-deploy/war.sha256 rc=1; was02 changed since install: petition.war")


def test_the_read_names_each_drift_line_by_its_record(tmp_path: Path) -> None:
    """Review rv-t4u4 L-D: the read body says which record a differing file came from (run under bash on a scratch
    record), and the parser refuses a drift line without one."""
    import hashlib
    import subprocess as sp

    body = vm_ops.READ_BODY
    function = body[body.index("check_sums() {"): body.index("\n}\n", body.index("check_sums() {")) + 3]
    base = tmp_path / "base"
    base.mkdir()
    (base / "a.conf").write_text("one\n")
    (base / "b.conf").write_text("two\n")
    record = tmp_path / "rec.sha256"
    stale, kept = hashlib.sha256(b"one").hexdigest(), hashlib.sha256((base / "b.conf").read_bytes()).hexdigest()
    record.write_text(f"{stale}  a.conf\n{kept}  b.conf\n")
    out = sp.run(["bash", "-c", function + f'check_sums "{record}" "{base}"'], capture_output=True, text=True).stdout
    assert out.splitlines() == [f"@@ sums {record} rc=1", f"@@ drift {record} a.conf"]

    text = _read_output(HOSTS_BY_NAME["web01"])
    with pytest.raises(VmOpsError, match="unexpected drift line"):
        vm_ops.parse_read(HOSTS_BY_NAME["web01"], text.replace("@@ end", "@@ drift /etc/x.conf\n@@ end"))
    facts = vm_ops.parse_read(HOSTS_BY_NAME["web01"], _read_output(HOSTS_BY_NAME["web01"], drift=("/etc/x.conf",)))
    assert facts.drift == ["/etc/x.conf"]
    assert facts.drift_records == {"/var/lib/petition-deploy/expected.sha256": ["/etc/x.conf"]}


def test_a_records_rc1_is_excused_only_when_its_own_lines_are_all_the_held_decoys(tmp_path: Path) -> None:
    """Review rv-t4u4 L-D (reproduced there): a held war decoy on was01 excuses war.sha256's line and its rc=1; an
    expected.sha256 rc=1 with no line of its own (a broken or empty record) still stops the injection."""
    remote = FakeRemote()
    clock = Clock()
    ops = _ops(tmp_path, remote, clock)
    remote.outputs["192.168.127.31"] = _read_output(
        HOSTS_BY_NAME["was01"],
        sums={"/var/lib/petition-deploy/war.sha256": "rc=1", "/var/lib/petition-deploy/expected.sha256": "rc=1"},
        drift=("petition.war",),
    )
    ops.read_all()
    war = {"was01": {"paths": {"/var/lib/tomcat10/webapps/petition.war"}, "guards": 1}}
    assert ops.drift_detail(excused=war) == "was01 /var/lib/petition-deploy/expected.sha256 rc=1"
    # Facts stored before the read named records: nothing is excused on the record line.
    document = ops.load()
    document["hosts"]["was01"]["facts"].pop("drift_records")
    ops.load = lambda: document
    assert "was01 /var/lib/petition-deploy/war.sha256 rc=1" in ops.drift_detail(excused=war)


# --- S testbed (civil-petition-sms, testbed-services docs/spec-testbed-sms-only.md, ST4) -------------------------

def sms_inventory_document() -> dict:
    """A test inventory in the shape of testbed-services civil-petition/deploy/inventory/s.json (made-up values
    where the real file is not needed; the real file is read by the test below when it is next to the runner)."""
    def host(name, role, addr, mgmt, net="biz"):
        return {"name": name, "role": role, "nics": [{"net": net, "addr": addr, "ifname": "ens18", "gateway": True},
                                                      {"net": "mgmt", "addr": mgmt, "ifname": "ens19"}]}
    return {
        "schema": "petition-portal-inventory/1", "testbed": "s", "domain": "civil-petition-sms",
        "accounts": {"executor": {"name": "opsjob", "uid": 1450, "pubkey_file": "~/.config/testbed/s5-executor.pub"}},
        "app": {"web_port": 80, "context": "petition"},
        "networks": {"biz": {"cidr": "10.31.0.0/24"}, "user": {"cidr": "10.32.0.0/24"},
                     "mgmt": {"cidr": "10.33.0.0/24", "management": True}},
        "router": None,
        "hosts": [host("websvr01", "web", "10.31.0.11", "10.33.0.11"), host("wassvr01", "was", "10.31.0.21", "10.33.0.21"),
                  host("wassvr02", "was", "10.31.0.22", "10.33.0.22"), host("dbsvr01", "db", "10.31.0.31", "10.33.0.31"),
                  host("loadgen", "loadgen", "10.32.0.10", "10.33.0.10", net="user")],
    }


def sms_domain(tmp_path: Path) -> vm_ops.VmDomain:
    path = tmp_path / "s.json"
    path.write_text(json.dumps(sms_inventory_document()))
    return vm_ops.load_inventory_domain(path, key=vm_ops.SMS_DOMAIN)


def test_the_s_testbed_is_read_from_its_inventory(tmp_path: Path) -> None:
    """ST4 contract 2: S's VMs, management addresses, agent names, loadgen, entry, key and known_hosts come from its
    inventory, nothing copied into the runner. No router, no 66 target, and the CLI is told the domain."""
    domain = sms_domain(tmp_path)
    assert domain.key == "civil-petition-sms" and not domain.router and domain.hypervisor_target == ""
    assert [(h.name, h.address, h.role) for h in domain.hosts] == [
        ("websvr01", "10.33.0.11", "web"), ("wassvr01", "10.33.0.21", "was"), ("wassvr02", "10.33.0.22", "was"),
        ("dbsvr01", "10.33.0.31", "db"), ("loadgen", "10.33.0.10", "loadgen")]
    assert dict(domain.agent_hosts) == {"websvr01": "10.31.0.11", "wassvr01": "10.31.0.21", "wassvr02": "10.31.0.22",
                                        "dbsvr01": "10.31.0.31"}
    assert domain.entry_url == "http://10.31.0.11:80/petition"
    assert domain.loadgen_live_path == "/tmp/k6-civil-petition-sms-live.json"
    assert domain.cli_args == ("--domain", "civil-petition-sms") and domain.state_subdir == "civil-petition-sms"
    web = domain.host("websvr01")
    assert web.key == vm_ops.KEY_DIR / "s5-executor" and web.user == "opsjob"
    assert web.known_hosts == vm_ops.KEY_DIR / "known_hosts_civil-petition-sms"
    assert web.units == vm_ops.ROLE_UNITS["web"] and web.files == vm_ops.PAIR_FILES
    assert dict(domain.host("dbsvr01").files)["archive_state"] == vm_ops.ARCHIVE_FACTS_PATH
    assert domain.host("loadgen").files == (("loadgen_live", "/tmp/k6-civil-petition-sms-live.json"), *vm_ops.PAIR_FILES)
    argv = vm_ops.executor_argv(web)
    assert "UserKnownHostsFile=" + str(vm_ops.KEY_DIR / "known_hosts_civil-petition-sms") in argv
    assert str(vm_ops.KEY_DIR / "s5-executor") in argv and argv[-5] == "opsjob@10.33.0.11"


def test_an_inventory_that_does_not_fit_is_refused(tmp_path: Path) -> None:
    document = sms_inventory_document()
    path = tmp_path / "s.json"
    for change, words in (
        (lambda d: d.update(domain="civil-petition"), "not 'civil-petition-sms'"),
        (lambda d: d["hosts"][0]["nics"].append({"net": "mgmt", "addr": "x"}), "2 management NICs"),
        (lambda d: d["networks"]["mgmt"].pop("management"), "0 management NICs"),
        (lambda d: d["hosts"][1]["nics"][0].pop("gateway"), "wassvr01 has no gateway NIC"),
        (lambda d: d["hosts"][0].update(role="cache"), "unknown role"),
        (lambda d: d["accounts"]["executor"].update(pubkey_file="k"), "does not end in .pub"),
        (lambda d: d["hosts"].pop(), "one web VM and a loadgen VM"),
        (lambda d: d.pop("app"), "unreadable"),
    ):
        changed = json.loads(json.dumps(document))
        change(changed)
        path.write_text(json.dumps(changed))
        with pytest.raises(VmOpsError, match=words):
            vm_ops.load_inventory_domain(path, key=vm_ops.SMS_DOMAIN)


INVENTORY_DIR = Path(__file__).resolve().parents[2].parent / "testbed-services/civil-petition/deploy/inventory"


def test_the_real_inventories_give_s_and_t_as_the_runner_reads_them() -> None:
    """The real s.json reads as five S VMs on the S management network; the real t.json, read the same way, gives
    exactly T's fixed table (so the loader's rules are T's rules, and T's table has not drifted from its inventory)."""
    if not (INVENTORY_DIR / "s.json").is_file():
        pytest.skip("testbed-services inventories are not checked out next to the runner")
    s = vm_ops.load_inventory_domain(INVENTORY_DIR / "s.json", key=vm_ops.SMS_DOMAIN)
    assert [h.name for h in s.hosts] == ["websvr01", "wassvr01", "wassvr02", "dbsvr01", "loadgen"]
    assert all(h.address.startswith("192.168.133.") for h in s.hosts)
    assert sorted(s.agent_hosts.values()) == ["192.168.131.11", "192.168.131.21", "192.168.131.22", "192.168.131.31"]
    assert s.entry_url == "http://192.168.131.11:80/petition" and not s.router
    t = vm_ops.load_inventory_domain(INVENTORY_DIR / "t.json", key=vm_ops.DOMAIN)
    fixed = vm_ops.T_DOMAIN
    assert [(h.name, h.address, h.role, h.units, h.files, h.user, h.key, h.known_hosts) for h in t.hosts] == [
        (h.name, h.address, h.role, h.units, h.files, h.user, h.key, h.known_hosts) for h in fixed.hosts]
    assert dict(t.agent_hosts) == dict(fixed.agent_hosts) and t.entry_url == fixed.entry_url and t.router
    assert vm_ops.server_pairs(fixed, s) == [("db01", "dbsvr01"), ("was01", "wassvr01"), ("was02", "wassvr02"),
                                             ("web01", "websvr01")]


WAR_LINE = "4f3c" * 16 + "  petition.war\n"
PAIR_NAMES = [("web01", "websvr01"), ("was01", "wassvr01"), ("was02", "wassvr02"), ("db01", "dbsvr01"),
              ("loadgen", "loadgen")]
PAIR_FILES_BY_T = {
    "web01": {"/etc/apache2/sites-available/petition.conf": "a1" * 32},
    "was01": {"/etc/tomcat10/server.xml": "b2" * 32, "/etc/default/tomcat10": "b3" * 32},
    # The batch node: its own files (testbed-services M2 contract fact ①, pairs differ inside one testbed).
    "was02": {"/etc/tomcat10/server.xml": "b9" * 32, "/etc/default/tomcat10": "b3" * 32,
              "/etc/systemd/system/petition-batch.timer": "ba" * 32},
    "db01": {"/etc/postgresql/16/main/postgresql.conf": "c3" * 32},
    "loadgen": {"/etc/systemd/system/loadgen.service": "d4" * 32},
}
T_ONLY = ["/etc/default/lldpd", "/etc/rsyslog.d/remote/10-forward.conf"]
LOADGEN_FILES = {"loadgen/script.js": "e1" * 32, "loadgen/entrypoint.sh": "e2" * 32}


def _expected_document() -> dict:
    return {"schema": "petition-pair-expected/1", "loadgen_files": LOADGEN_FILES, "pairs": [
        {"t": t, "s": s, "role": "loadgen" if t == "loadgen" else t.rstrip("0123456789"), "files": PAIR_FILES_BY_T[t],
         "t_only": [] if t == "loadgen" else T_ONLY,
         "differs": ["/etc/netplan/01-host.yaml"] if t == "loadgen" else []} for t, s in PAIR_NAMES]}


def _normalized(files: dict) -> str:
    return "".join(f"{value}  {dest}\n" for dest, value in sorted(files.items()))


def _installed(t_name: str, *, testbed: str) -> dict:
    """What the record step leaves on a VM: the pair's files, T's T-only files, a netplan that differs on purpose,
    and the load scripts on the loadgen."""
    files = dict(PAIR_FILES_BY_T[t_name])
    if testbed == "t" and t_name != "loadgen":
        files.update({dest: "70" * 32 for dest in T_ONLY})
    if t_name == "loadgen":
        files["/etc/netplan/01-host.yaml"] = ("71" if testbed == "t" else "72") * 32
        files.update(LOADGEN_FILES)
    return files


def _pair_ops(tmp_path: Path, t_out: dict, s_out: dict, *, s_age_min: int = 0, expected: dict | None = None):
    """T and S VmOps after one periodic read each, and the expected file; t_out/s_out give _read_output overrides
    per VM name (their «files» replace the right records)."""
    remote_t, remote_s = FakeRemote(), FakeRemote()
    clock = Clock()
    tmp_path.mkdir(parents=True, exist_ok=True)
    domain = sms_domain(tmp_path)
    expected_path = tmp_path / "pair-expected.json"
    expected_path.write_text(json.dumps(expected if expected is not None else _expected_document()))
    twin = {s: t for t, s in PAIR_NAMES}

    for remote, hosts, out, testbed in ((remote_t, HOSTS, t_out, "t"), (remote_s, domain.hosts, s_out, "s")):
        for host in hosts:
            t_name = host.name if testbed == "t" else twin.get(host.name)
            files = {}
            if t_name in PAIR_FILES_BY_T:
                files["pair_normalized"] = _normalized(_installed(t_name, testbed=testbed))
            if host.role == "was":
                files["pair_war"] = WAR_LINE
            override = dict(out.get(host.name, {}))
            override["files"] = {**files, **override.get("files", {})}
            remote.outputs[host.address] = _read_output(host, **override)
    t_ops = _ops(tmp_path / "t", remote_t, clock)
    s_ops = VmOps(domain=domain, process_runner=remote_s, clock=clock, state_dir=tmp_path / "s",
                  supervisor=MasterSupervisor(domain.hosts, process_runner=remote_s, clock=clock,
                                              state_dir=tmp_path / "s", control_dir=tmp_path / "cms",
                                              popen=lambda *a, **k: None))
    t_ops.read_all()
    clock.value -= timedelta(minutes=s_age_min)
    s_ops.read_all()
    clock.value += timedelta(minutes=s_age_min)
    return t_ops, s_ops, expected_path


def test_the_pair_holds_only_when_both_testbeds_installed_the_one_expected_value(tmp_path: Path) -> None:
    """Review rv-st4r M2 and ST4 contract M2 (S design §2-4 condition 1, §7-4 «the expected value is one, T's»):
    pair by pair, each VM's normalized checksums without the pair's t_only and differs files are exactly the
    pair's files, the loadgens carry the expected load scripts, S's WAS carry T's one WAR record line, and nothing
    changed since install. Anything else is «no pair» with why; a held decoy's file is excused as in the drift
    gate."""
    t_ops, s_ops, expected = _pair_ops(tmp_path / "a", {}, {})
    verdict = vm_ops.pair_verdict(t_ops, s_ops, expected_path=expected)
    assert verdict["pair"] is True and verdict["differences"] == [], verdict
    assert len(verdict["checked"]) == 10 and "civil-petition-sms/loadgen" in verdict["checked"]

    site = "/etc/apache2/sites-available/petition.conf"
    other_was = {**_installed("was01", testbed="s"), "/etc/tomcat10/server.xml": "ee" * 32}
    cases = {
        # S installed from another commit: its own records pass, its normalized checksums are not the pair's.
        "wassvr01: normalized checksums differ from the expected ones for /etc/tomcat10/server.xml": (
            {}, {"wassvr01": {"files": {"pair_normalized": _normalized(other_was)}}}),
        # T changed too: T is held to the same one value.
        "db01: normalized checksums differ from the expected ones for /etc/postgresql/16/main/extra.conf": (
            {"db01": {"files": {"pair_normalized": _normalized({**_installed("db01", testbed="t"),
                                                                 "/etc/postgresql/16/main/extra.conf": "0" * 64})}}}, {}),
        "loadgen: load scripts differ from the expected ones (loadgen/entrypoint.sh, loadgen/script.js)": (
            {}, {"loadgen": {"files": {"pair_normalized": _normalized({**_installed("loadgen", testbed="s"),
                                                                        "loadgen/script.js": "ff" * 32})}}}),
        "web01: no /var/lib/petition-deploy/normalized.sha256": ({"web01": {"files": {"pair_normalized": None}}}, {}),
        "civil-petition-sms WAR record is not civil-petition's": (
            {}, {"wassvr02": {"files": {"pair_war": "9a" * 32 + "  petition.war\n"}}}),
        "civil-petition WAS carry 2 WAR record lines, expected one": (
            {"was02": {"files": {"pair_war": "9b" * 32 + "  petition.war\n"}}}, {}),
        "no WAR record on was01": ({"was01": {"files": {"pair_war": None}}}, {}),
        "websvr01 changed since install: " + site: (
            {}, {"websvr01": {"sums": {"/var/lib/petition-deploy/expected.sha256": "rc=1"}, "drift": (site,)}}),
        "wassvr01 tomcat10.service failed": ({}, {"wassvr01": {"units": {"tomcat10.service": "failed"}}}),
    }
    for index, (words, (t_out, s_out)) in enumerate(cases.items()):
        t_ops, s_ops, expected = _pair_ops(tmp_path / f"c{index}", t_out, s_out)
        verdict = vm_ops.pair_verdict(t_ops, s_ops, expected_path=expected)
        assert verdict["pair"] is False and any(item.startswith(words) for item in verdict["differences"]), (
            words, verdict["differences"])
    held_out = {"websvr01": {"sums": {"/var/lib/petition-deploy/expected.sha256": "rc=1"}, "drift": (site,)}}
    t_ops, s_ops, expected = _pair_ops(tmp_path / "held", {}, held_out)
    held = {"websvr01": {"paths": {site}, "guards": 1}}
    assert vm_ops.pair_verdict(t_ops, s_ops, excused=held, expected_path=expected)["pair"] is True
    t_ops, s_ops, expected = _pair_ops(tmp_path / "stale", {}, {}, s_age_min=30)
    verdict = vm_ops.pair_verdict(t_ops, s_ops, expected_path=expected)
    assert verdict["pair"] is False and "websvr01: no fresh periodic read" in verdict["differences"]
    # A VM the expected values do not pair, no expected value, or a broken one: no pair, with why.
    document = _expected_document()
    document["pairs"] = [row for row in document["pairs"] if row["s"] != "dbsvr01"]
    t_ops, s_ops, expected = _pair_ops(tmp_path / "unpaired", {}, {}, expected=document)
    verdict = vm_ops.pair_verdict(t_ops, s_ops, expected_path=expected)
    assert verdict["pair"] is False and "dbsvr01: no pair in the expected values" in verdict["differences"]
    t_ops, s_ops, expected = _pair_ops(tmp_path / "noexp", {}, {})
    expected.unlink()
    verdict = vm_ops.pair_verdict(t_ops, s_ops, expected_path=expected)
    assert verdict["pair"] is False and "pair expected values unreadable" in verdict["differences"][0]
    t_ops, s_ops, expected = _pair_ops(tmp_path / "bad", {}, {}, expected={"pairs": [], "loadgen_files": {}})
    verdict = vm_ops.pair_verdict(t_ops, s_ops, expected_path=expected)
    assert verdict["pair"] is False and "have no pairs" in verdict["differences"][0]


def test_the_pair_is_no_pair_with_why_while_either_testbed_is_off(monkeypatch) -> None:
    monkeypatch.setattr(vm_ops, "get_vm_ops", lambda: None)
    monkeypatch.setattr(vm_ops, "get_sms_ops", lambda: None)
    verdict = vm_ops.current_pair_verdict()
    assert verdict["pair"] is False and "civil-petition, civil-petition-sms" in verdict["differences"][0]


def test_s_reads_its_own_vms_with_its_own_cli_domain_and_state(tmp_path: Path, monkeypatch) -> None:
    """The S VmOps reads only S's five VMs (no 66: S has no executors there), keeps its facts and routine in its own
    folder, gives the S db VM's archive line to its own archive folder, and tells the operator CLI the domain."""
    monkeypatch.setattr(vm_ops, "STATE_DIR", tmp_path / "state")
    domain = sms_domain(tmp_path)
    remote = FakeRemote()
    for host in domain.hosts:
        remote.outputs[host.address] = _read_output(host, files={"archive_state": '{"ok": 1}'})
    ops = VmOps(domain=domain, process_runner=remote, clock=Clock(), archive_state_dir=tmp_path / "archive",
                supervisor=MasterSupervisor(domain.hosts, process_runner=remote, clock=Clock(),
                                            state_dir=tmp_path / "x", control_dir=tmp_path / "cm",
                                            popen=lambda *a, **k: None))
    assert ops.state_dir == tmp_path / "state" / "civil-petition-sms" and ops.hypervisor_target == ""
    ops.read_all()
    targets = {argv[-5] for argv, _ in remote.calls if "-O" not in argv}
    assert targets == {f"opsjob@{h.address}" for h in domain.hosts}
    assert not any(argv[-1].startswith("hvjob@") for argv, _ in remote.calls)
    assert json.loads((tmp_path / "archive" / "last.json").read_text()) == {"ok": 1}
    calls = []
    routine = vm_ops.OperatorRoutine(process_runner=lambda argv, **k: calls.append(argv) or subprocess.CompletedProcess(
        argv, 0, '["websvr01"]', ""), state_dir=tmp_path, cli_args=domain.cli_args, clock=Clock())
    assert routine.nodes() == ("websvr01",)
    assert calls[0][2:] == ["--domain", "civil-petition-sms", "--list-nodes"]
    # Only the node list names the domain; a session names its node (unique across the testbeds, ST4 contract 2).
    routine._call(["--node", "websvr01", "--check"])
    assert calls[1][2:] == ["--node", "websvr01", "--check"]
    assert ops.routine.cli_args == ("--domain", "civil-petition-sms")


def test_the_real_expected_values_pair_the_real_inventories() -> None:
    """The real pair-expected.json (testbed-services, next to the inventories) reads as five pairs in the runner's
    twin order with the load scripts, so the runner's reading and the render's file agree."""
    path = INVENTORY_DIR / "pair-expected.json"
    if not path.is_file():
        pytest.skip("testbed-services pair-expected.json is not checked out next to the runner")
    expected = vm_ops.load_pair_expected(path)
    s = vm_ops.load_inventory_domain(INVENTORY_DIR / "s.json", key=vm_ops.SMS_DOMAIN)
    pairs = [(row["t"], row["s"]) for row in expected["pairs"]]
    assert sorted(pairs[:4]) == sorted(vm_ops.server_pairs(vm_ops.T_DOMAIN, s)) and pairs[4] == ("loadgen", "loadgen")
    assert sorted(expected["loadgen_files"]) == ["loadgen/entrypoint.sh", "loadgen/script.js"]
    assert vm_ops.PAIR_EXPECTED.name == path.name and vm_ops.PAIR_EXPECTED.parent.name == "inventory"
