from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

from app.live_probes import APPROVED_ORACLE_TAGS, HOST_PROBE_CONTRACTS, LiveProbeSet
from app.observations import ApprovedQueryRegistry

NOW = datetime(2026, 7, 17, 3, 0, tzinfo=timezone.utc)


class FakeProcess:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append((argv, kwargs))
        if argv[0] == "ssh":
            return subprocess.CompletedProcess(
                argv, 0,
                json.dumps({"active": True, "clean": False, "filesystem_used_percent": 87}).encode(),
                b"",
            )
        if "testbed-oracle-0" in argv:
            return subprocess.CompletedProcess(argv, 0, "1\n", "")
        if "testbed-mysql-0" in argv:
            return subprocess.CompletedProcess(argv, 0, "0\n", "")
        raise AssertionError(argv)


def probes(fake: FakeProcess) -> LiveProbeSet:
    return LiveProbeSet(
        process_runner=fake,
        http_client=lambda *args, **kwargs: {},
        database_client=lambda *args, **kwargs: {},
        database_credentials={},
        clock=lambda: NOW,
    )


def observe(fake: FakeProcess, query_id: str, parameters: dict):
    query = ApprovedQueryRegistry.from_path().bind(
        {"query_id": query_id, "parameters": parameters}
    )
    return probes(fake).observe(query)


def test_oracle_and_mysql_queries_are_fixed_kubectl_reads() -> None:
    fake = FakeProcess()
    oracle = observe(fake, "database.oracle_tagged_session_count", {
        "client_identifier": "rca-F01-P-oracle-lock"
    })
    mysql = observe(fake, "database.mysql_index_present", {
        "database": "fooddelivery", "table": "menus", "index": "idx_menus_category"
    })
    assert oracle["quality"] == "good" and oracle["value"] == 1
    assert mysql["quality"] == "good" and mysql["value"] is False
    rendered = json.dumps([argv for argv, _ in fake.calls])
    assert "v$session" in rendered and "information_schema.statistics" in rendered
    assert all(call[0][0:2] == ["kubectl", "--kubeconfig"] for call in fake.calls)


def test_oracle_probe_reads_every_approved_lock_tag_not_just_f01p() -> None:
    # The tag used to be frozen into one contract dict and hand-encoded as
    # chr() codes in the SQL, so F08-G and F15-G had no observable Oracle lock.
    for tag in sorted(APPROVED_ORACLE_TAGS):
        fake = FakeProcess()
        result = observe(fake, "database.oracle_tagged_session_count", {
            "client_identifier": tag
        })
        assert result["quality"] == "good"
        rendered = json.dumps([argv for argv, _ in fake.calls])
        # The tag reaches the query as chr()-joined codes, never as a raw quote.
        assert "||".join(f"chr({ord(ch)})" for ch in tag) in rendered
        assert f"'{tag}'" not in rendered


def test_oracle_probe_rejects_a_tag_outside_the_approved_set() -> None:
    fake = FakeProcess()
    result = observe(fake, "database.oracle_tagged_session_count", {
        "client_identifier": "rca-F99-Z-oracle-lock"
    })
    assert result["quality"] == "error"


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
