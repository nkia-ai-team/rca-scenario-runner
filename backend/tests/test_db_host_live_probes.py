from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.live_probes import (
    APPROVED_ORACLE_TAGS,
    DB_PROBE_CLIENT,
    DB_PROBE_PYTHON,
    HOST_PROBE_CONTRACTS,
    HOST_PROBE_JOBS,
    HOST_PROBE_SCRIPT,
    MYSQL_INDEX_CONTRACT,
    LiveProbeSet,
)
from app.observations import ApprovedQueryRegistry

NOW = datetime(2026, 7, 17, 3, 0, tzinfo=timezone.utc)


SECRETS = {
    "oracle-secret": {
        "ORACLE_PASSWORD": "sys-pw-not-for-probes", "APP_USER": "banking", "APP_USER_PASSWORD": "app-pw-1",
        "MON_USER": "LUCIDA_MON", "MON_PASSWORD": "mon-pw-2",
    },
    "mysql-secret": {
        "MYSQL_ROOT_PASSWORD": "root-pw-not-for-probes", "MYSQL_USER": "fooddelivery", "MYSQL_PASSWORD": "food-pw-3",
    },
}
PASSWORDS = [v for data in SECRETS.values() for k, v in data.items() if "PASSWORD" in k]


# Stand-ins for the database drivers the real probe client imports (python-oracledb, PyMySQL). The fake process runs
# DB_PROBE_CLIENT itself with these on its path, so every probe test goes through the client's own connect, execute,
# fetch and print (C6b test audit 다2: the old fake_sqlplus imitated the real tool's output; a fake that returns bare
# values once hid four probes that read nothing, 2026-07-30). Each call is logged as one JSON line in FAKE_DB_LOG.
_FAKE_DRIVER = """
import json, os

class OperationalError(Exception):
    pass

class DatabaseError(Exception):
    pass

def _log(entry):
    with open(os.environ["FAKE_DB_LOG"], "a") as fh:
        fh.write(json.dumps(entry) + "\\n")

class _Cursor:
    def execute(self, sql, binds=None):
        _log({"kind": "execute", "sql": sql, "binds": binds})
    def fetchone(self):
        answer = os.environ["FAKE_DB_ANSWER"]
        return (int(answer) if answer.lstrip("-").isdigit() else answer,)

class _Connection:
    module = None
    def __init__(self, **kwargs):
        if os.environ.get("FAKE_DB_FAIL"):
            raise OperationalError(os.environ["FAKE_DB_FAIL"] + "\\nsecond line that must not be reported")
        self.kwargs = kwargs
    def connect(self):
        _log({"kind": "connect", "attrs": getattr(self, "_connect_attrs", None)})
    def cursor(self):
        _log({"kind": "connect-kwargs", "kwargs": {k: v for k, v in self.kwargs.items() if k != "password"},
              "module": self.module})
        return _Cursor()
    def close(self):
        pass

def connect(**kwargs):
    return _Connection(**kwargs)
"""
_FAKE_DRIVER_DIR = tempfile.mkdtemp(prefix="fake-db-drivers-")
Path(_FAKE_DRIVER_DIR, "oracledb.py").write_text(_FAKE_DRIVER)
Path(_FAKE_DRIVER_DIR, "pymysql").mkdir()
Path(_FAKE_DRIVER_DIR, "pymysql", "__init__.py").write_text(_FAKE_DRIVER)


class FakeProcess:
    """kubectl answers reads only; the database probe client really runs, against fake drivers that answer from the
    engine's entry in ANSWERS.

    Any `kubectl exec` fails the test: the probes must not run inside a database pod
    (v4 C6b; the old sqlplus / mysql exec left a runc exec, a client process and a SYS
    session from the DB host in what 119 records)."""

    def __init__(self, answers: dict[str, str] | None = None, fail_client: bool = False) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.answers = answers or {"oracle": "1", "mysql": "0"}
        self.fail_client = fail_client
        self.db_log = Path(tempfile.mkstemp(prefix="fake-db-log-")[1])

    def db_calls(self) -> list[dict]:
        return [json.loads(line) for line in self.db_log.read_text().splitlines()]

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        if argv[0] == "ssh":
            return subprocess.CompletedProcess(
                argv, 0,
                json.dumps({"active": True, "clean": False, "filesystem_used_percent": 87}).encode(),
                b"",
            )
        if argv[0] == DB_PROBE_PYTHON:
            assert argv[1:] == ["-c", DB_PROBE_CLIENT], argv
            env = dict(kwargs["env"], PYTHONPATH=_FAKE_DRIVER_DIR, FAKE_DB_LOG=str(self.db_log),
                       FAKE_DB_ANSWER=self.answers[kwargs["env"]["DBP_ENGINE"]])
            if self.fail_client:
                env["FAKE_DB_FAIL"] = "(1045, \"Access denied for user 'fooddelivery'\")"
            done = subprocess.run([sys.executable, "-c", argv[2]], env=env, capture_output=True,
                                  text=True, timeout=kwargs.get("timeout", 15))
            if kwargs.get("check") and done.returncode != 0:
                raise subprocess.CalledProcessError(done.returncode, argv, done.stdout, done.stderr)
            return subprocess.CompletedProcess(argv, done.returncode, done.stdout, done.stderr)
        assert argv[0:2] == ["kubectl", "--kubeconfig"], argv
        assert "exec" not in argv, argv
        verb, kind = argv[3], argv[4]
        assert verb == "get", argv
        if kind == "nodes":
            return subprocess.CompletedProcess(argv, 0, json.dumps({"items": NODES}), "")
        name = argv[5]
        if kind == "pod":
            assert argv[-1] == "jsonpath={.spec.nodeName}", argv
            node = {"testbed-oracle-0": "tb-w2", "testbed-mysql-0": "tb-w3"}[name]
            return subprocess.CompletedProcess(argv, 0, node, "")
        if kind == "service":
            port = {"testbed-oracle-external": "30308", "testbed-mysql-external": "30236"}[name]
            return subprocess.CompletedProcess(argv, 0, port, "")
        if kind == "secret":
            # One key at a time: the probes read only their accounts' keys.
            key = argv[-1].removeprefix("jsonpath={.data.").removesuffix("}")
            assert argv[-1] == "jsonpath={.data." + key + "}", argv
            return subprocess.CompletedProcess(argv, 0, base64.b64encode(SECRETS[name][key].encode()).decode(), "")
        raise AssertionError(argv)

    def client_calls(self) -> list[dict]:
        return [kwargs["env"] for argv, kwargs in self.calls if argv[0] == DB_PROBE_PYTHON]


def _node(name: str, address: str, control_plane: bool = False, ready: bool = True) -> dict:
    labels = {"kubernetes.io/hostname": name}
    if control_plane:
        labels["node-role.kubernetes.io/control-plane"] = ""
    return {"metadata": {"name": name, "labels": labels},
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                       "addresses": [{"type": "InternalIP", "address": address}]}}


# 66 (z4-66-survey): tb-cp .77, tb-w1 .184, tb-w2 .11 (banking), tb-w3 .14 (food).
NODES = [_node("tb-cp", "192.168.122.77", control_plane=True), _node("tb-w1", "192.168.122.184"),
         _node("tb-w2", "192.168.122.11"), _node("tb-w3", "192.168.122.14")]


def probes(fake: FakeProcess) -> LiveProbeSet:
    return LiveProbeSet(
        process_runner=fake,
        http_client=lambda *args, **kwargs: {},
        database_client=lambda *args, **kwargs: {},
        database_credentials={},
        clock=lambda: NOW,
    )


def observe(fake: FakeProcess, query_id: str, parameters: dict, probe_set: LiveProbeSet | None = None):
    query = ApprovedQueryRegistry.from_path().bind(
        {"query_id": query_id, "parameters": parameters}
    )
    return (probe_set or probes(fake)).observe(query)


ORACLE_PROBES = {
    "database.oracle_tagged_session_count": {"client_identifier": "dba-maintenance"},
    "database.outbox_unpublished_count": {"namespace": "rca-testbed-banking"},
    "database.integrity_violation_count": {"since": "2026-07-30 05:00:00"},
    "database.ledger_unmatched_transfer_count": {"window_minutes": 5, "grace_seconds": 60},
}
# Least privilege per probe: v$session needs the read-only DPM account (the application
# schema account gets ORA-00942, Oracle Free 23 measured 2026-11-10); the banking tables
# are the application schema's own; the index probe reads the application's own database.
EXPECTED_USER = {
    "database.oracle_tagged_session_count": "LUCIDA_MON",
    "database.outbox_unpublished_count": "banking",
    "database.integrity_violation_count": "banking",
    "database.ledger_unmatched_transfer_count": "banking",
    "database.mysql_index_present": "fooddelivery",
}


def test_database_probes_are_network_sessions_with_least_privilege_accounts() -> None:
    cases = dict(ORACLE_PROBES)
    cases["database.mysql_index_present"] = MYSQL_INDEX_CONTRACT
    for query_id, parameters in cases.items():
        fake = FakeProcess()
        result = observe(fake, query_id, parameters)
        assert result["quality"] == "good", (query_id, result)
        if query_id == "database.mysql_index_present":
            assert result["value"] is False
        else:
            assert result["value"] == 1, (query_id, result)
        (env,) = fake.client_calls()
        engine = "mysql" if query_id == "database.mysql_index_present" else "oracle"
        assert env["DBP_ENGINE"] == engine
        assert env["DBP_USER"] == EXPECTED_USER[query_id], query_id
        # Through the control plane, the way the runner's load enters (v4 C6b 수정 2회차): the database sees the
        # runner's own address, never the DPM collector's (the database node) nor the injections' (a worker that runs
        # neither the database nor a coupled domain).
        assert env["DBP_HOST"] == "192.168.122.77"
        assert env["DBP_PORT"] == ("30236" if engine == "mysql" else "30308")
        # Never the administrator passwords; the account's own one only in the child's env.
        assert env["DBP_PASSWORD"] not in {"sys-pw-not-for-probes", "root-pw-not-for-probes"}
        for argv, _ in fake.calls:
            assert "exec" not in argv
            for password in PASSWORDS:
                assert all(password not in item for item in argv), (query_id, argv)
        client_argv = [argv for argv, _ in fake.calls if argv[0] == DB_PROBE_PYTHON]
        assert client_argv == [[DB_PROBE_PYTHON, "-c", DB_PROBE_CLIENT]]


def test_probe_client_really_connects_executes_once_and_prints_the_value() -> None:
    # C6b test audit 다2: the fake drivers stand where python-oracledb and PyMySQL are; the client itself runs. One
    # connect with the operator tool's identity, one execute with exactly the probe's SQL and binds, the first
    # column of the one row printed; a driver error is one line on stderr without the password.
    cases = dict(ORACLE_PROBES)
    cases["database.mysql_index_present"] = MYSQL_INDEX_CONTRACT
    for query_id, parameters in cases.items():
        fake = FakeProcess()
        result = observe(fake, query_id, parameters)
        assert result["quality"] == "good", (query_id, result)
        (env,) = fake.client_calls()
        calls = fake.db_calls()
        executes = [c for c in calls if c["kind"] == "execute"]
        assert len(executes) == 1, (query_id, calls)
        assert executes[0]["sql"] == env["DBP_SQL"]
        assert executes[0]["binds"] == json.loads(env["DBP_BINDS"])
        (connected,) = [c for c in calls if c["kind"] == "connect-kwargs"]
        if env["DBP_ENGINE"] == "oracle":
            assert {k: connected["kwargs"][k] for k in ("program", "machine", "osuser", "driver_name")} == {
                "program": "SQLcl", "machine": "ops-bastion", "osuser": "dba", "driver_name": "jdbcthin : 23.7.0.25.1"}
            assert connected["kwargs"]["dsn"] == f"{env['DBP_HOST']}:{env['DBP_PORT']}/{env['DBP_SERVICE']}"
            assert connected["module"] == "SQLcl"
        else:
            (attrs,) = [c["attrs"] for c in calls if c["kind"] == "connect"]
            assert attrs["program_name"] == "mysql" and attrs["_client_name"] == "libmysql"
            assert connected["kwargs"]["database"] == "fooddelivery"
        assert "password" not in json.dumps(calls)
    fake = FakeProcess(fail_client=True)
    failed = observe(fake, "database.mysql_index_present", MYSQL_INDEX_CONTRACT)
    assert failed["quality"] == "error"
    assert "OperationalError: (1045" in json.dumps(failed, default=str)
    assert "second line" not in json.dumps(failed, default=str)
    for password in PASSWORDS:
        assert password not in json.dumps(failed, default=str)


def test_mysql_index_probe_reads_the_index_catalogue() -> None:
    # C6b test audit 다1: the index probe reads information_schema.statistics (a fixed SELECT count(*)), the old
    # kubectl-read test's second half.
    fake = FakeProcess()
    observe(fake, "database.mysql_index_present", MYSQL_INDEX_CONTRACT)
    (env,) = fake.client_calls()
    assert "information_schema.statistics" in env["DBP_SQL"]
    assert re.fullmatch(r"(?is)\s*select count\(\*\) from [^;]+", env["DBP_SQL"]), env["DBP_SQL"]


def test_probe_client_carries_the_operator_tool_identity_and_no_value() -> None:
    # Same identity as the testbed-services injectors (oracle_lock_remote.py constants,
    # db_operator_session.MYSQL_CONNECT_ATTRS); nothing that names a scenario.
    for fragment in ('program="SQLcl"', 'machine="ops-bastion"', 'osuser="dba"',
                     'driver_name="jdbcthin : 23.7.0.25.1"', 'terminal="unknown"', 'conn.module = "SQLcl"',
                     '"_client_name": "libmysql"', '"program_name": "mysql"', "defer_connect=True"):
        assert fragment in DB_PROBE_CLIENT, fragment
    assert not re.search(r"(?i)rca|F\d\d-|scenario|sysdba|as sysdba", DB_PROBE_CLIENT)


def test_oracle_probe_reads_every_approved_lock_tag_not_just_f01p() -> None:
    # The tag used to be frozen into one contract dict and hand-encoded as chr() codes in
    # the SQL (it rode a shell pipeline into sqlplus). It is now a bind variable.
    for tag in sorted(APPROVED_ORACLE_TAGS):
        fake = FakeProcess()
        result = observe(fake, "database.oracle_tagged_session_count", {
            "client_identifier": tag
        })
        assert result["quality"] == "good"
        (env,) = fake.client_calls()
        assert json.loads(env["DBP_BINDS"]) == {"tag": tag}
        assert ":tag" in env["DBP_SQL"] and tag not in env["DBP_SQL"]
        assert "v$session" in env["DBP_SQL"]


def test_oracle_probe_rejects_a_tag_outside_the_approved_set() -> None:
    fake = FakeProcess()
    result = observe(fake, "database.oracle_tagged_session_count", {
        "client_identifier": "rca-F99-Z-oracle-lock"
    })
    assert result["quality"] == "error"
    assert fake.calls == []


def test_probe_values_bind_rather_than_splice() -> None:
    fake = FakeProcess()
    observe(fake, "database.integrity_violation_count", {"since": "2026-07-30 05:00:00"})
    observe(fake, "database.ledger_unmatched_transfer_count", {"window_minutes": 5, "grace_seconds": 60})
    observe(fake, "database.mysql_index_present", MYSQL_INDEX_CONTRACT)
    integrity, ledger, mysql = fake.client_calls()
    assert json.loads(integrity["DBP_BINDS"]) == {"since": "2026-07-30 05:00:00"}
    assert "2026-07-30" not in integrity["DBP_SQL"] and ":since" in integrity["DBP_SQL"]
    assert json.loads(ledger["DBP_BINDS"]) == {"window": 5, "grace": 60}
    assert ":window" in ledger["DBP_SQL"] and ":grace" in ledger["DBP_SQL"]
    assert json.loads(mysql["DBP_BINDS"]) == MYSQL_INDEX_CONTRACT
    assert "idx_menus_category" not in mysql["DBP_SQL"]


def test_listener_and_secret_are_read_once_per_probe_set_and_again_after_a_failure() -> None:
    fake = FakeProcess()
    probe_set = probes(fake)
    for _ in range(3):
        result = observe(fake, "database.oracle_tagged_session_count",
                         {"client_identifier": "dba-maintenance"}, probe_set)
        assert result["value"] == 1
    kubectl = [argv for argv, _ in fake.calls if argv[0] == "kubectl"]
    # node list, service nodePort and the keys of the two account roles, once. Never the administrator password
    # (C6b review: the whole Secret used to be decoded).
    keys = sorted(argv[-1].removeprefix("jsonpath={.data.").removesuffix("}") for argv in kubectl if argv[4] == "secret")
    assert keys == ["APP_USER", "APP_USER_PASSWORD", "MON_PASSWORD", "MON_USER"], keys
    assert len(kubectl) == 2 + len(keys), kubectl
    fake.fail_client = True
    failed = observe(fake, "database.oracle_tagged_session_count",
                     {"client_identifier": "dba-maintenance"}, probe_set)
    assert failed["quality"] == "error"
    for password in PASSWORDS:
        assert password not in json.dumps(failed, default=str)
    fake.fail_client = False
    observe(fake, "database.oracle_tagged_session_count", {"client_identifier": "dba-maintenance"}, probe_set)
    # A failed query dropped the cache, so the next probe read everything again (twice the first reads).
    assert len([argv for argv, _ in fake.calls if argv[0] == "kubectl"]) == 2 * len(kubectl)


def test_unparsable_probe_output_is_an_error_not_a_value() -> None:
    fake = FakeProcess(answers={"oracle": "Session altered.", "mysql": "2"})
    assert observe(fake, "database.oracle_tagged_session_count",
                   {"client_identifier": "dba-maintenance"})["quality"] == "error"
    assert observe(fake, "database.mysql_index_present", MYSQL_INDEX_CONTRACT)["quality"] == "error"


def test_host_queries_use_only_measured_worker_and_fixed_probe_script() -> None:
    for scenario_id, (host, mode, target) in HOST_PROBE_CONTRACTS.items():
        fake = FakeProcess()
        active = observe(fake, "host.scenario_active", {"scenario_id": scenario_id})
        clean = observe(fake, "host.scenario_clean", {"scenario_id": scenario_id})
        assert active["value"] is True and clean["value"] is False
        for argv, kwargs in fake.calls:
            assert f"nkia@{host}" in argv
            # sudo logs exactly what follows it to the worker's auth log (119 syslog, the recording):
            # nothing may follow `bash -s` (design principle 8). Until 2026-11-12 the scenario id,
            # mode and target did.
            assert argv[-3:] == ["sudo", "bash", "-s"]
            assert "StrictHostKeyChecking=yes" in argv
            assert scenario_id not in " ".join(argv)
            stdin = kwargs["input"]
            assert scenario_id.encode() not in stdin and scenario_id.lower().encode() not in stdin
            preamble, body = stdin.split(b"\n", 1)
            assert body == HOST_PROBE_SCRIPT and body.startswith(b"#!/usr/bin/env bash")
            # The worker's bash reads mode and target from the preamble, quoted as given.
            received = subprocess.run(["bash", "-s"], input=preamble + b'\nprintf "%s\\n" "$@"\n',
                                      capture_output=True, timeout=30).stdout.decode().split("\n")
            assert received[:2] == [mode, target]


def _host_probe(state_root: Path, mode: str, target: str) -> dict:
    preamble = ("set -- " + " ".join(shlex.quote(arg) for arg in (mode, target)) + "\n").encode()
    done = subprocess.run(["bash", "-s"], input=preamble + HOST_PROBE_SCRIPT, capture_output=True, timeout=60,
                          env={**os.environ, "OPSJOB_STATE_ROOT": str(state_root)})
    assert done.returncode == 0, done.stderr.decode()
    return json.loads(done.stdout.decode())


def test_host_probe_finds_the_job_by_the_names_host_stress_gives_it() -> None:
    """The probe reads the pid and artifact files host.stress leaves (testbed-services
    profiles/host_stress_executor.py, mode -> job name since 2026-10-16), not per-scenario names, so
    the worker needs no scenario id to answer and its files name nothing."""
    assert HOST_PROBE_JOBS == {"fio": "iobench", "watermark": "bulkcopy", "pressure": "soak"}
    for mode, job in HOST_PROBE_JOBS.items():
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state, target = root / "opsjobs", root / "volume"
            state.mkdir(); target.mkdir()
            probe_target = "" if mode == "pressure" else str(target)
            idle = _host_probe(state, mode, probe_target)
            assert (idle["active"], idle["clean"]) == (False, True), (mode, idle)
            sleeper = subprocess.Popen(["sleep", "30"])
            try:
                (state / f"{job}.pid").write_text(f"{sleeper.pid}\n")
                if mode == "fio":
                    (target / f".{job}.dat").write_bytes(b"x")
                if mode == "watermark":
                    (target / f".{job}.part").write_bytes(b"x")
                busy = _host_probe(state, mode, probe_target)
            finally:
                sleeper.kill(); sleeper.wait()
            assert (busy["active"], busy["clean"]) == (True, False), (mode, busy)
            # The pre-2026-10-16 per-scenario names mean nothing to the probe any more.
            for leftover in state.iterdir():
                leftover.unlink()
            for leftover in target.iterdir():
                leftover.unlink()
            (state / "F10-H.pid").write_text(f"{os.getpid()}\n")
            (target / ".lucida-F10-H-fio").write_bytes(b"x")
            assert _host_probe(state, mode, probe_target)["clean"] is True


def test_filesystem_query_rejects_non_storage_target_and_unknown_scenario() -> None:
    fake = FakeProcess()
    value = observe(fake, "host.filesystem_used_percent", {"scenario_id": "F10-R"})
    assert value["quality"] == "good" and value["value"] == 87
    blocked = observe(fake, "host.filesystem_used_percent", {"scenario_id": "F15-P"})
    assert blocked["quality"] == "error" and blocked["value"] is None
    unknown = observe(fake, "host.scenario_active", {"scenario_id": "F10-G"})
    assert unknown["quality"] == "error" and unknown["value"] is None
