from __future__ import annotations

import base64
import json
import re
import subprocess
from datetime import datetime, timezone

from app.live_probes import (
    APPROVED_ORACLE_TAGS,
    DB_PROBE_CLIENT,
    DB_PROBE_PYTHON,
    HOST_PROBE_CONTRACTS,
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


class FakeProcess:
    """kubectl answers reads only; the database probe client answers from DBP_* env.

    Any `kubectl exec` fails the test: the probes must not run inside a database pod
    (v4 C6b; the old sqlplus / mysql exec left a runc exec, a client process and a SYS
    session from the DB host in what 119 records)."""

    def __init__(self, answers: dict[str, str] | None = None, fail_client: bool = False) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.answers = answers or {"oracle": "1", "mysql": "0"}
        self.fail_client = fail_client

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
            if self.fail_client:
                raise subprocess.CalledProcessError(
                    1, argv, "", "OperationalError: (1045, \"Access denied for user 'fooddelivery'\")\n"
                )
            return subprocess.CompletedProcess(argv, 0, self.answers[kwargs["env"]["DBP_ENGINE"]] + "\n", "")
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
        # Through a worker that runs neither database pod nor the control plane (user decision Q8): tb-w1 for both
        # (the database nodes are tb-w2 and tb-w3), never the database node itself (the DPM collector's address)
        # nor tb-cp (the runner load's).
        assert env["DBP_HOST"] == "192.168.122.184"
        assert env["DBP_PORT"] == ("30236" if engine == "mysql" else "30308")
        # Never the administrator passwords; the account's own one only in the child's env.
        assert env["DBP_PASSWORD"] not in {"sys-pw-not-for-probes", "root-pw-not-for-probes"}
        for argv, _ in fake.calls:
            assert "exec" not in argv
            for password in PASSWORDS:
                assert all(password not in item for item in argv), (query_id, argv)
        client_argv = [argv for argv, _ in fake.calls if argv[0] == DB_PROBE_PYTHON]
        assert client_argv == [[DB_PROBE_PYTHON, "-c", DB_PROBE_CLIENT]]


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
    # pod node, node list, service nodePort and the keys of the two account roles, once. Never the
    # administrator password (C6b review: the whole Secret used to be decoded).
    keys = sorted(argv[-1].removeprefix("jsonpath={.data.").removesuffix("}") for argv in kubectl if argv[4] == "secret")
    assert keys == ["APP_USER", "APP_USER_PASSWORD", "MON_PASSWORD", "MON_USER"], keys
    assert len(kubectl) == 3 + len(keys), kubectl
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
            assert argv[-3:] == [scenario_id, mode, target]
            assert kwargs["input"].startswith(b"#!/usr/bin/env bash")
            assert "StrictHostKeyChecking=yes" in argv


def test_filesystem_query_rejects_non_storage_target_and_unknown_scenario() -> None:
    fake = FakeProcess()
    value = observe(fake, "host.filesystem_used_percent", {"scenario_id": "F10-R"})
    assert value["quality"] == "good" and value["value"] == 87
    blocked = observe(fake, "host.filesystem_used_percent", {"scenario_id": "F15-P"})
    assert blocked["quality"] == "error" and blocked["value"] is None
    unknown = observe(fake, "host.scenario_active", {"scenario_id": "F10-G"})
    assert unknown["quality"] == "error" and unknown["value"] is None
