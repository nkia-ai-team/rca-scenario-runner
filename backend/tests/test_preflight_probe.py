from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone

from app.preflight import build_preflight_checks, evaluate_preflight
from app.preflight_probe import ProductionPreflightProbe


def _fake_urlopen(payloads):
    @contextmanager
    def urlopen(url, timeout=None):
        class Response:
            pass

        # The trace probe posts a Request with the SQL as its body; VM queries
        # pass a plain URL string. Match on whichever carries the identifier.
        if isinstance(url, str):
            haystack = url
        else:
            haystack = url.full_url + (url.data or b"").decode()
        for fragment, payload in payloads.items():
            if fragment in haystack:
                response = Response()
                response.read = lambda payload=payload: json.dumps(payload).encode()
                yield response
                return
        raise AssertionError(f"unexpected url: {url}")

    return urlopen


def _fake_process_runner(stdout):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    return run


def test_collect_maps_units_and_feeds_a_clean_deterministic_verdict(monkeypatch):
    monkeypatch.setenv("LUCIDA_LOGIN_USER", "u")
    monkeypatch.setenv("LUCIDA_LOGIN_PASSWORD", "p")

    def zero_incidents(**kwargs):
        return 0

    monkeypatch.setattr("app.preflight_probe.open_incident_count", zero_incidents)
    urlopen = _fake_urlopen(
        {
            "percentile95": {"data": {"result": [{"value": [0, "0.08747"]}]}},
            "otel_traces_local": {"data": [{"value": 0.0}]},
            "pending_requests": {"data": {"result": []}},
            "/api/v1/alerts": {"data": {"alerts": []}},
        }
    )
    probe = ProductionPreflightProbe(
        vm_url="http://vm",
        urlopen=urlopen,
        process_runner=_fake_process_runner(
            "active\nJul 21 running baseline [ 38% ] 02/20 VUs 4.00 iters/s\n"
        ),
    )

    observations = probe.collect(now=datetime.now(timezone.utc))

    assert observations["baseline_loadgen_alive"] == 1.0
    assert observations["achieved_rps"] == 4.0
    assert observations["entry_p95_sec"] == 0.08747
    assert observations["prev_pool_pending"] == 0.0
    verdict = evaluate_preflight(
        window=("2026-07-21T00:00:00Z", "2026-07-21T00:10:00Z"),
        checks=build_preflight_checks(observations),
        now=datetime.now(timezone.utc),
    )
    assert verdict.verdict == "clean"


def test_dead_loadgen_or_open_incident_fails_the_gate(monkeypatch):
    monkeypatch.setenv("LUCIDA_LOGIN_USER", "u")
    monkeypatch.setenv("LUCIDA_LOGIN_PASSWORD", "p")
    monkeypatch.setattr("app.preflight_probe.open_incident_count", lambda **kwargs: 2)
    urlopen = _fake_urlopen(
        {
            "percentile95": {"data": {"result": [{"value": [0, "0.09"]}]}},
            "otel_traces_local": {"data": [{"value": 0.0}]},
            "pending_requests": {"data": {"result": []}},
            "/api/v1/alerts": {"data": {"alerts": [{"state": "firing"}]}},
        }
    )
    probe = ProductionPreflightProbe(
        vm_url="http://vm", urlopen=urlopen,
        process_runner=_fake_process_runner("inactive\n"),
    )

    observations = probe.collect(now=datetime.now(timezone.utc))

    assert observations["baseline_loadgen_alive"] == 0.0
    assert observations["achieved_rps"] == 0.0
    assert observations["active_alarms"] == 1.0
    assert observations["open_incidents"] == 2.0
    verdict = evaluate_preflight(
        window=("2026-07-21T00:00:00Z", "2026-07-21T00:10:00Z"),
        checks=build_preflight_checks(observations),
        now=datetime.now(timezone.utc),
    )
    assert verdict.verdict == "dirty"


def test_queue_filter_removes_decoy_alarms_and_keeps_the_rest(monkeypatch):
    """U7: the queue lends the probe a filter for one collect(); what it
    excuses leaves active_alarms and is kept for the verdict record. Without a
    filter every firing alarm counts, as before."""
    monkeypatch.setattr("app.preflight_probe.open_incident_count", lambda **kwargs: 0)
    decoy_alert = {"state": "firing", "labels": {"service_name": "commerce-order"}}
    real_alert = {"state": "firing", "labels": {"service_name": "commerce-payment"}}
    urlopen = _fake_urlopen(
        {
            "percentile95": {"data": {"result": [{"value": [0, "0.09"]}]}},
            "otel_traces_local": {"data": [{"value": 0.0}]},
            "pending_requests": {"data": {"result": []}},
            "/api/v1/alerts": {"data": {"alerts": [decoy_alert, real_alert, {"state": "inactive"}]}},
        }
    )
    probe = ProductionPreflightProbe(
        vm_url="http://vm", urlopen=urlopen,
        process_runner=_fake_process_runner("active\n4.00 iters/s\n"),
    )

    assert probe.collect(now=datetime.now(timezone.utc))["active_alarms"] == 2.0
    assert probe.last_excused_alarms == []

    def only_decoys(alerts):
        keep = [a for a in alerts if a["labels"] != decoy_alert["labels"]]
        return keep, [{"decoy_id": "d1"}]

    probe.set_alarm_filter(only_decoys)
    assert probe.collect(now=datetime.now(timezone.utc))["active_alarms"] == 1.0
    assert probe.last_excused_alarms == [{"decoy_id": "d1"}]


# --- VM testbed (civil-petition): testbed-services docs/spec-testbed-vm-3tier.md §6-4 ----------------------------

import base64 as _base64
import urllib.parse as _urlparse
from datetime import timedelta as _timedelta
from pathlib import Path as _Path

import pytest as _pytest

from app import vm_ops as _vm_ops
from app.preflight_probe import VmDomainPreflightProbe, vm_domain_rps_floor, vm_domain_target_rps
from tests.test_vm_ops import NOW as _VM_NOW, Clock as _VmClock, FakeRemote as _VmRemote, _hypervisor, _read_output

_ROUTER_ID = "65b4ab9c-d1b4-42f6-be5c-7efb5c34cb3c"
_LOADGEN_ENV = "PEAK_RPS=30 TROUGH_RPS=5 LOADGEN_SEED=42 ENTRY_URL=http://192.168.124.11:80/petition"


class _VmDomainRemote(_VmRemote):
    """The periodic read and the 66 helper (test_vm_ops.FakeRemote) plus the loadgen's live-summary read."""

    def __init__(self, clock) -> None:
        super().__init__()
        self.clock = clock
        self.loadgen_state = "active"
        self.live: dict | None = None

    def live_document(self, **overrides) -> dict:
        document = {
            "domain": "civil-petition", "unit": "loadgen", "achieved_rps": 17.2, "http_count": 1032,
            "http_abnormal_rate": 0.0, "http_p95_ms": 8.6,
            "observed_at": (self.clock.value - _timedelta(seconds=4)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        document.update(overrides)
        return document

    def __call__(self, argv, **kwargs):
        stdin = kwargs.get("input") or b""
        if isinstance(stdin, bytes) and b"systemctl show -p Environment" in stdin:
            self.calls.append((list(argv), stdin))
            live = self.live if self.live is not None else self.live_document()
            payload = _base64.b64encode(json.dumps(live).encode()).decode()
            out = (
                "@@ host loadgen\n"
                f"@@ unit loadgen.service {self.loadgen_state}\n"
                f"@@ env {_LOADGEN_ENV}\n"
                f"@@ file loadgen_live {payload}\n@@ end\n"
            )
            return subprocess.CompletedProcess(argv, 0, out.encode(), b"")
        return super().__call__(argv, **kwargs)


def _vm_urlopen(answers: dict):
    """Fake VictoriaMetrics: the first answer whose key is inside the decoded PromQL."""

    @contextmanager
    def urlopen(url, timeout=None):
        class Response:
            pass

        text = url if isinstance(url, str) else url.full_url
        query = _urlparse.parse_qs(_urlparse.urlparse(text).query).get("query", [""])[0]
        for fragment, payload in answers.items():
            if fragment in query or fragment in text:
                response = Response()
                response.read = lambda payload=payload: json.dumps(payload).encode()
                yield response
                return
        raise AssertionError(f"unexpected query: {query or text}")

    return urlopen


def _rows(values: dict) -> dict:
    return {"status": "success", "data": {"result": [
        {"metric": {"host_name": host} if host else {}, "value": [0, str(value)]} for host, value in values.items()
    ]}}


_SERVERS = ("192.168.124.11", "192.168.125.11", "192.168.125.12", "192.168.126.11")


def _vm_answers(*, cpu_age=None, icmp=None, icmp_age=None, snmp=1, snmp_age=30, alerts=()):
    return {
        'sms.cpu.utilization': _rows(cpu_age or {host: 15 for host in _SERVERS}),
        'time() - max by (host_name) (tlast_over_time({__name__="sms.server.icmp_status"': _rows(
            icmp_age or {host: 20 for host in _SERVERS}),
        'max by (host_name) (last_over_time({__name__="sms.server.icmp_status"': _rows(
            icmp or {host: 1 for host in _SERVERS}),
        f'time() - max(tlast_over_time({{__name__="nms.network.snmp_status",target_id="{_ROUTER_ID}"}}': _rows(
            {"": snmp_age} if snmp_age is not None else {}),
        f'max(last_over_time({{__name__="nms.network.snmp_status",target_id="{_ROUTER_ID}"}}': _rows(
            {"": snmp} if snmp is not None else {}),
        "pending_requests": _rows({"": 0}),
        "/api/v1/alerts": {"data": {"alerts": list(alerts)}},
    }


def _vm_probe(tmp_path: _Path, *, answers=None, reads: int = 3, settle_min: int = 60):
    """A probe on real VmOps state: `reads` periodic reads five minutes apart (router /proc/stat busy 25% between
    them, 66 counters 30 MB each way per 5 minutes), all masters opened `settle_min` minutes before the last read."""
    clock = _VmClock(_VM_NOW - _timedelta(minutes=5 * (reads - 1)))
    remote = _VmDomainRemote(clock)
    tmp_path.mkdir(parents=True, exist_ok=True)
    targets = tmp_path / "t.targets.json"
    targets.write_text(json.dumps({"network_devices": [{"name": "rt-core01", "address": "192.168.123.254",
                                                        "target_id": _ROUTER_ID}]}))
    ops = _vm_ops.VmOps(
        process_runner=remote, clock=clock, state_dir=tmp_path / "vm-ops", hypervisor_target="hvjob@192.168.122.1",
        supervisor=_vm_ops.MasterSupervisor(
            (*_vm_ops.HOSTS, _vm_ops.hypervisor_host("hvjob@192.168.122.1")), process_runner=remote, clock=clock,
            state_dir=tmp_path / "vm-ops", control_dir=tmp_path / "cm", popen=lambda *a, **k: None),
    )
    for index in range(reads):
        busy, idle = 100 + 250 * index, 800 + 750 * index
        remote.outputs["192.168.127.2"] = _read_output(
            _vm_ops.HOSTS_BY_NAME["rt-core01"], at=clock.value, procstat=f"cpu  {busy} 0 0 {idle} 0 0 0 0 0 0")
        remote.hypervisor = _hypervisor(clock.value, rx=30_000_000 * index, tx=30_000_000 * index)
        ops.read_all()
        if index < reads - 1:
            clock.value += _timedelta(minutes=5)
    # The last routine operator session (Q35 condition ②) succeeded 20 minutes before the probe.
    (tmp_path / "vm-ops" / "operator-routine.json").write_text(json.dumps({"last_ok": {
        "at": (clock.value - _timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ"), "host": "was01"}}))
    opened = (clock.value - _timedelta(minutes=settle_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ops.supervisor.status = {host.name: {"alive": True, "opened_at": opened, "detail": ""} for host in ops.supervisor.hosts}
    clock.value += _timedelta(minutes=5)  # the injection-time fresh 66 read is 5 minutes after the last periodic one
    remote.hypervisor = _hypervisor(clock.value, rx=30_000_000 * reads, tx=30_000_000 * reads)
    probe = VmDomainPreflightProbe(
        ops=ops, vm_url="http://vm", urlopen=_vm_urlopen(answers or _vm_answers()), incident_count=lambda: 0,
        targets_path=targets,
    )
    return probe, ops, remote, clock


def _vm_gate(observations, now):
    return evaluate_preflight(
        window=("2026-11-20T05:50:00Z", "2026-11-20T06:00:00Z"),
        checks=build_preflight_checks(observations, domain="civil-petition"),
        now=now,
    )


def test_vm_domain_probe_reads_every_signal_and_passes_a_healthy_testbed(tmp_path) -> None:
    probe, ops, remote, clock = _vm_probe(tmp_path)
    observations = probe.collect(now=clock.value)
    assert observations["baseline_loadgen_alive"] == 1.0
    assert observations["achieved_rps"] == 17.2
    # 15:05 KST: hour 15 target 5 + 25*98% = 30 (rounded), hour 14 target 28; floor half the lower.
    assert observations["diurnal_rps_floor"] == 14.0
    assert observations["user_5xx_rate"] == 0.0 and observations["entry_p95_sec"] == _pytest.approx(0.0086)
    assert observations["tunnel_handshake_age_sec"] == 12 and observations["tunnel_ping_lost"] == 0
    # 10 minutes back: two reads of 30 MB each way = 120 MB over 600 s = 1.6 Mbit/s.
    assert observations["tunnel_load_mbps"] == _pytest.approx(120_000_000 * 8 / 600 / 1e6)
    assert observations["router_cpu_pct"] == _pytest.approx(25.0)
    assert observations["mgmt_ssh_down"] == 0 and observations["mgmt_ssh_settle_sec"] == 65 * 60
    assert observations["clock_offset_ms"] == _pytest.approx(0.012345)
    assert observations["sms_cpu_sample_age_sec"] == 15 and observations["router_snmp_down"] == 0
    assert observations["operator_routine_age_sec"] == 20 * 60
    verdict = _vm_gate(observations, clock.value)
    assert verdict.verdict == "clean", [c for c in verdict.checks if c.status != "pass"]
    # The loadgen read and every VM read go over the shared socket as the executor account.
    loadgen_calls = [argv for argv, stdin in remote.calls if isinstance(stdin, bytes) and b"systemctl show -p Environment" in stdin]
    assert loadgen_calls and all("opsjob@192.168.127.10" in argv and "ControlMaster=no" in argv for argv in loadgen_calls)


def test_vm_domain_probe_goes_dirty_when_the_tunnel_is_cut(tmp_path) -> None:
    """§10 T4 preflight: a tunnel cut on purpose makes the gate dirty (no handshake, every ping lost)."""
    probe, ops, remote, clock = _vm_probe(tmp_path)
    remote.hypervisor = {**_hypervisor(clock.value, rx=90_000_000, tx=90_000_000), "handshake_age_sec": None,
                         "ping": {"sent": 10, "received": 0, "max_ms": None}}
    verdict = _vm_gate(probe.collect(now=clock.value), clock.value)
    assert verdict.verdict == "dirty"
    assert {c.name for c in verdict.checks if c.status == "fail"} == {
        "tunnel_handshake_age_sec", "tunnel_ping_lost", "tunnel_ping_max_ms"}


def test_vm_domain_probe_reads_the_helpers_never_handshake_value_as_none(tmp_path) -> None:
    """Review rv-t4r3 M1: the helper answers -1 when there was never a handshake. With every ping answered the
    handshake check alone must still fail (it used to pass -1 <= 150)."""
    probe, ops, remote, clock = _vm_probe(tmp_path)
    remote.hypervisor = {**_hypervisor(clock.value, rx=90_000_000, tx=90_000_000), "handshake_age_sec": None}
    observations = probe.collect(now=clock.value)
    assert observations["tunnel_handshake_age_sec"] == 1_000_000 and observations["tunnel_ping_lost"] == 0
    assert ops.load()["hypervisor"]["history"][-1]["handshake_age_sec"] is None
    verdict = _vm_gate(observations, clock.value)
    assert {c.name for c in verdict.checks if c.status == "fail"} == {"tunnel_handshake_age_sec"}
    # Any negative age is «none» too.
    assert _vm_ops._handshake_age(-5) is None and _vm_ops._handshake_age(0) == 0 and _vm_ops._handshake_age(None) is None


def test_vm_domain_probe_goes_dirty_when_a_management_connection_is_down_or_just_reopened(tmp_path) -> None:
    """§10 T4 preflight: an always-open connection cut on purpose makes the gate dirty; once reopened the gate
    stays dirty for 30 minutes (the reopen's new connection must not sit at the front of a recording)."""
    probe, ops, remote, clock = _vm_probe(tmp_path)
    ops.supervisor.status["db01"] = {"alive": False, "opened_at": None, "detail": "could not open: No route to host"}
    verdict = _vm_gate(probe.collect(now=clock.value), clock.value)
    assert {c.name for c in verdict.checks if c.status == "fail"} == {"mgmt_ssh_down", "mgmt_ssh_settle_sec"}
    ops.supervisor.status["db01"] = {"alive": True, "detail": "",
                                     "opened_at": (clock.value - _timedelta(minutes=29)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    verdict = _vm_gate(probe.collect(now=clock.value), clock.value)
    assert {c.name for c in verdict.checks if c.status == "fail"} == {"mgmt_ssh_settle_sec"}
    clock.value += _timedelta(minutes=1, seconds=1)
    remote.hypervisor = _hypervisor(clock.value, rx=90_000_000, tx=90_000_000)
    assert _vm_gate(probe.collect(now=clock.value), clock.value).verdict == "clean"
    # 66's master counts too (T4S contract 6: the 66 executors need it).
    assert "pve66" in ops.supervisor.status
    ops.supervisor.status["pve66"] = {"alive": False, "opened_at": None, "detail": "could not open"}
    verdict = _vm_gate(probe.collect(now=clock.value), clock.value)
    assert {c.name for c in verdict.checks if c.status == "fail"} == {"mgmt_ssh_down", "mgmt_ssh_settle_sec"}


def test_vm_domain_probe_counts_guards_clock_freshness_and_router_load(tmp_path) -> None:
    answers = _vm_answers(cpu_age={host: 15 for host in _SERVERS[:3]}, icmp={**{h: 1 for h in _SERVERS}, _SERVERS[3]: 0},
                          snmp=0, snmp_age=200)
    probe, ops, remote, clock = _vm_probe(tmp_path, answers=answers)
    # A guard still armed on was02 (one undo, its timer) and one on 66.
    remote.outputs["192.168.127.32"] = _read_output(_vm_ops.HOSTS_BY_NAME["was02"], at=clock.value, guards=(1, 1))
    remote.outputs["192.168.127.2"] = _read_output(_vm_ops.HOSTS_BY_NAME["rt-core01"], at=clock.value,
                                                   procstat="cpu  2000 0 0 2300 0 0 0 0 0 0",
                                                   chrony=vm_ops_chrony(0.0612))
    ops.read_all()
    remote.hypervisor = {**_hypervisor(clock.value, rx=90_000_000, tx=90_000_000), "guards_active": 1}
    observations = probe.collect(now=clock.value)
    assert observations["residual_guards"] == 2
    assert observations["clock_offset_ms"] == _pytest.approx(61.2)
    # db01 (126.11) has no CPU sample in the window: age = lookback + 1, never a pass.
    assert observations["sms_cpu_sample_age_sec"] == 901
    assert observations["sms_icmp_down"] == 1
    assert observations["router_snmp_down"] == 1 and observations["router_snmp_sample_age_sec"] == 200
    # (2000-600) busy and no idle since the previous read (busy 600, idle 2300).
    assert observations["router_cpu_pct"] == _pytest.approx(100.0)
    failing = {c.name for c in _vm_gate(observations, clock.value).checks if c.status == "fail"}
    assert failing == {"residual_guards", "clock_offset_ms", "sms_cpu_sample_age_sec", "sms_icmp_down",
                       "router_snmp_down", "router_snmp_sample_age_sec", "router_cpu_pct"}


def vm_ops_chrony(offset_sec: float) -> str:
    return f"7F7F0101,192.168.127.1,3,1795076000.1,{offset_sec:.9f},0,0,0,0,0,0,0,64,Normal"


def test_vm_domain_probe_refuses_stale_or_foreign_inputs_with_the_reason(tmp_path) -> None:
    probe, ops, remote, clock = _vm_probe(tmp_path)
    # The periodic read stopped answering for web01: its last error travels in the reason (U3).
    remote.outputs["192.168.127.21"] = RuntimeError("ssh: connect to host 192.168.127.21 port 22: Connection timed out")
    clock.value += _timedelta(seconds=_vm_ops.FACTS_MAX_AGE_SEC)
    ops.read_all()
    remote.hypervisor = _hypervisor(clock.value, rx=90_000_000, tx=90_000_000)
    remote.live = remote.live_document()
    with _pytest.raises(_vm_ops.VmOpsError, match="web01: last read .* Connection timed out"):
        probe.collect(now=clock.value)

    probe, ops, remote, clock = _vm_probe(tmp_path / "b")
    remote.live = remote.live_document(observed_at=(clock.value - _timedelta(seconds=45)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    with _pytest.raises(_vm_ops.VmOpsError, match="45s old"):
        probe.collect(now=clock.value)
    remote.live = remote.live_document(domain="commerce")
    with _pytest.raises(_vm_ops.VmOpsError, match="not this domain"):
        probe.collect(now=clock.value)
    remote.live = {k: v for k, v in remote.live_document().items() if k not in ("http_abnormal_rate", "http_p95_ms")}
    with _pytest.raises(_vm_ops.VmOpsError, match="no requests in its window"):
        probe.collect(now=clock.value)
    # A gap in the 66 counter history: no 10-15 minute baseline, no judgement.
    probe, ops, remote, clock = _vm_probe(tmp_path / "c", reads=1)
    with _pytest.raises(_vm_ops.VmOpsError, match="no tunnel counter sample 600-900s"):
        probe.collect(now=clock.value)


def test_vm_domain_floor_follows_the_loadgen_hour_table() -> None:
    # entrypoint.sh: TROUGH + ((PEAK-TROUGH)*pct + 50)/100, KST hour.
    assert [vm_domain_target_rps(hour, _LOADGEN_ENV) for hour in (4, 10, 16)] == [5, 18, 30]
    assert vm_domain_rps_floor(datetime(2026, 11, 20, 7, 30, tzinfo=timezone.utc), _LOADGEN_ENV) == 15.0  # 16 KST
    assert vm_domain_rps_floor(datetime(2026, 11, 19, 19, 30, tzinfo=timezone.utc), _LOADGEN_ENV) == 2.5   # 04 KST
    with _pytest.raises(_vm_ops.VmOpsError, match="PEAK_RPS"):
        vm_domain_target_rps(3, "LOADGEN_SEED=42")


# --- Safety rules the review mutants showed unpinned (rv-t4r2 M3, L5) --------------------------------------------

def test_vm_domain_guard_residue_counts_a_left_timer_without_a_state_file(tmp_path) -> None:
    """rv-t4r2 R5: T4S contract 8 is «no *.unit and no run-r* timer». A timer left without its state file (an undo
    that failed half way) is residue too."""
    probe, ops, remote, clock = _vm_probe(tmp_path)
    remote.outputs["192.168.127.32"] = _read_output(_vm_ops.HOSTS_BY_NAME["was02"], at=clock.value, guards=(0, 1))
    remote.outputs["192.168.127.2"] = _read_output(_vm_ops.HOSTS_BY_NAME["rt-core01"], at=clock.value,
                                                   procstat="cpu  850 0 0 3050 0 0 0 0 0 0")
    ops.read_all()
    remote.hypervisor = _hypervisor(clock.value, rx=90_000_000, tx=90_000_000)
    assert probe.collect(now=clock.value)["residual_guards"] == 1


def test_vm_domain_preflight_refuses_stale_reads_even_when_they_showed_drift(tmp_path) -> None:
    """rv-t4r2 R24, the other half: the drift gate leaves stale reads out (test_vm_ops), and preflight then stops
    the domain on the staleness itself, so a stale read never opens the way."""
    probe, ops, remote, clock = _vm_probe(tmp_path)
    remote.outputs["192.168.127.31"] = _read_output(_vm_ops.HOSTS_BY_NAME["was01"], at=clock.value, drift=("petition.war",))
    ops.read_all()
    clock.value += _timedelta(seconds=_vm_ops.FACTS_MAX_AGE_SEC + 1)
    remote.hypervisor = _hypervisor(clock.value, rx=90_000_000, tx=90_000_000)
    assert ops.drift_detail() is None
    with _pytest.raises(_vm_ops.VmOpsError, match="periodic management read is stale: rt-core01: last read"):
        probe.collect(now=clock.value)


def test_vm_domain_probe_refuses_a_scenario_loadgen_document(tmp_path) -> None:
    """rv-t4r2 R16: a live summary that carries a scenario identity is a scenario's own k6 output, not the resident
    baseline, and is never judged as the domain's normal load."""
    probe, ops, remote, clock = _vm_probe(tmp_path)
    remote.live = remote.live_document(scenario_id="F99-X")
    with _pytest.raises(_vm_ops.VmOpsError, match="not this domain's baseline document"):
        probe.collect(now=clock.value)


def test_vm_domain_probe_refuses_a_short_ping_run(tmp_path) -> None:
    """rv-t4r2 R17: fewer than the 10 pings the design asks for cannot show «10 pings, loss 0»."""
    probe, ops, remote, clock = _vm_probe(tmp_path)
    remote.hypervisor = {**_hypervisor(clock.value, rx=90_000_000, tx=90_000_000),
                         "ping": {"sent": 5, "received": 5, "max_ms": 1.0}}
    with _pytest.raises(_vm_ops.VmOpsError, match="sent 5 pings, expected 10"):
        probe.collect(now=clock.value)


def test_vm_domain_probe_refuses_a_missing_master_status(tmp_path) -> None:
    """rv-t4r2 R18: a supervised host with no status at all (66 here) is not counted as alive or settled."""
    probe, ops, remote, clock = _vm_probe(tmp_path)
    del ops.supervisor.status["pve66"]
    with _pytest.raises(_vm_ops.VmOpsError, match=r"no master status for \['pve66'\]"):
        probe.collect(now=clock.value)


def test_vm_domain_preflight_stops_when_the_routine_operator_session_stopped(tmp_path) -> None:
    """Q35 condition ②: with no routine operator session for two of its longest gaps (or none ever), operator
    logins would happen only at injections, so this domain's preflight is dirty."""
    probe, ops, remote, clock = _vm_probe(tmp_path)
    (tmp_path / "vm-ops" / "operator-routine.json").write_text(json.dumps({"last_ok": {
        "at": (clock.value - _timedelta(seconds=10801)).strftime("%Y-%m-%dT%H:%M:%SZ"), "host": "db01"}}))
    verdict = _vm_gate(probe.collect(now=clock.value), clock.value)
    assert {c.name for c in verdict.checks if c.status == "fail"} == {"operator_routine_age_sec"}
    (tmp_path / "vm-ops" / "operator-routine.json").unlink()
    assert probe.collect(now=clock.value)["operator_routine_age_sec"] == 10801


def test_vm_domain_guard_residue_excuses_one_guard_per_held_decoy_on_its_own_node(tmp_path) -> None:
    """Command-session decision D1 (b): a VM decoy held through the recording keeps one executor guard on its node.
    That one is no residue; a second guard there, or any guard on another node, still is."""
    probe, ops, remote, clock = _vm_probe(tmp_path)
    remote.outputs["192.168.127.32"] = _read_output(_vm_ops.HOSTS_BY_NAME["was02"], at=clock.value, guards=(1, 1))
    remote.outputs["192.168.127.2"] = _read_output(_vm_ops.HOSTS_BY_NAME["rt-core01"], at=clock.value,
                                                   procstat="cpu  850 0 0 3050 0 0 0 0 0 0")
    ops.read_all()
    remote.hypervisor = _hypervisor(clock.value, rx=90_000_000, tx=90_000_000)
    assert probe.collect(now=clock.value)["residual_guards"] == 1
    probe.set_held_decoys({"was02": {"paths": set(), "guards": 1}})
    assert probe.collect(now=clock.value)["residual_guards"] == 0
    probe.set_held_decoys({"was01": {"paths": set(), "guards": 1}})  # another node's decoy excuses nothing here
    assert probe.collect(now=clock.value)["residual_guards"] == 1
    probe, ops, remote, clock = _vm_probe(tmp_path / "two")
    remote.outputs["192.168.127.32"] = _read_output(_vm_ops.HOSTS_BY_NAME["was02"], at=clock.value, guards=(2, 2))
    remote.outputs["192.168.127.2"] = _read_output(_vm_ops.HOSTS_BY_NAME["rt-core01"], at=clock.value,
                                                   procstat="cpu  850 0 0 3050 0 0 0 0 0 0")
    ops.read_all()
    remote.hypervisor = _hypervisor(clock.value, rx=90_000_000, tx=90_000_000)
    probe.set_held_decoys({"was02": {"paths": set(), "guards": 1}, "was01": {"paths": set(), "guards": 3}})
    assert probe.collect(now=clock.value)["residual_guards"] == 1
    probe.set_held_decoys({})
    assert probe.collect(now=clock.value)["residual_guards"] == 2


# --- S testbed (civil-petition-sms, S design §7-4) ---------------------------------------------------------------
from tests.test_vm_ops import sms_domain as _sms_domain  # noqa: E402

_SMS_LOADGEN_ENV = "PEAK_RPS=30 TROUGH_RPS=5 LOADGEN_SEED=42 ENTRY_URL=http://10.31.0.11:80/petition"


class _SmsRemote(_VmDomainRemote):
    def live_document(self, **overrides) -> dict:
        return super().live_document(domain="civil-petition-sms", **overrides)


def _sms_probe(tmp_path: _Path, *, answers=None, reads: int = 3, settle_min: int = 60):
    """An S probe on real S VmOps state next to the T probe whose 66 read carries the shared tunnel."""
    t_probe, t_ops, t_remote, clock = _vm_probe(tmp_path / "t", reads=reads)
    domain = _sms_domain(tmp_path)
    remote = _SmsRemote(clock)
    remote.outputs = {host.address: _read_output(host, at=clock.value) for host in domain.hosts}
    ops = _vm_ops.VmOps(
        domain=domain, process_runner=remote, clock=clock, state_dir=tmp_path / "s-ops",
        supervisor=_vm_ops.MasterSupervisor(domain.hosts, process_runner=remote, clock=clock,
                                            state_dir=tmp_path / "s-ops", control_dir=tmp_path / "cms",
                                            popen=lambda *a, **k: None),
    )
    ops.read_all()
    (tmp_path / "s-ops" / "operator-routine.json").write_text(json.dumps({"last_ok": {
        "at": (clock.value - _timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ"), "host": "wassvr01"}}))
    opened = (clock.value - _timedelta(minutes=settle_min)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ops.supervisor.status = {host.name: {"alive": True, "opened_at": opened, "detail": ""} for host in ops.supervisor.hosts}
    agents = sorted(domain.agent_hosts.values())
    default = {
        "sms.cpu.utilization": _rows({host: 15 for host in agents}),
        'time() - max by (host_name) (tlast_over_time({__name__="sms.server.icmp_status"': _rows({h: 20 for h in agents}),
        'max by (host_name) (last_over_time({__name__="sms.server.icmp_status"': _rows({h: 1 for h in agents}),
        "pending_requests": _rows({"": 0}),
        "/api/v1/alerts": {"data": {"alerts": []}},
    }
    probe = VmDomainPreflightProbe(
        ops=ops, tunnel_ops=t_ops, vm_url="http://vm", urlopen=_vm_urlopen(answers or default),
        incident_count=lambda: 0,
    )
    return probe, ops, remote, t_remote, clock


def _sms_gate(observations, now):
    return evaluate_preflight(
        window=("2026-11-20T05:50:00Z", "2026-11-20T06:00:00Z"),
        checks=build_preflight_checks(observations, domain="civil-petition-sms"),
        now=now,
    )


def test_the_s_probe_reads_every_s_signal_on_s_vms_and_t_s_tunnel(tmp_path) -> None:
    """S design §7-4: its loadgen, its five VMs' reads and masters, its four SMS agents' freshness at 119, and the
    tunnel through T's 66 read. No router row; 66's guards are T's executors', not counted for S."""
    probe, ops, remote, t_remote, clock = _sms_probe(tmp_path)
    t_remote.hypervisor = {**t_remote.hypervisor, "guards_active": 2}
    observations = probe.collect(now=clock.value)
    from app.preflight import SMS_VM_DOMAIN_REQUIRED_SIGNALS

    assert set(observations) == SMS_VM_DOMAIN_REQUIRED_SIGNALS
    assert observations["residual_guards"] == 0 and observations["mgmt_ssh_down"] == 0
    assert observations["tunnel_handshake_age_sec"] == 12
    assert _sms_gate(observations, clock.value).verdict == "clean"
    # Every S read and the loadgen read went to S's management addresses with S's known_hosts, never to T or 66.
    targets = {argv[-5] for argv, _ in remote.calls if "-O" not in argv}
    assert targets == {f"opsjob@{host.address}" for host in ops.hosts}
    # The loadgen summary is S's own file (the loadgen unit names it by the domain value).
    loadgen = [stdin for argv, stdin in remote.calls if isinstance(stdin, bytes) and b"systemctl show -p Environment" in stdin]
    assert loadgen and all(b"/tmp/k6-civil-petition-sms-live.json" in stdin and b"k6-civil-petition-live" not in stdin
                           for stdin in loadgen)
    assert all(f"UserKnownHostsFile={_vm_ops.KEY_DIR / 'known_hosts_civil-petition-sms'}" in argv
               for argv, _ in remote.calls)
    # An S VM's own armed guard is residue.
    remote.outputs["10.33.0.21"] = _read_output(ops.domain.host("wassvr01"), at=clock.value, guards=(1, 1))
    ops.read_all()
    assert probe.collect(now=clock.value)["residual_guards"] == 1


def test_a_cut_tunnel_makes_the_s_gate_dirty(tmp_path) -> None:
    """§11 ST4: the tunnel cut on purpose (no handshake, pings lost) is a dirty S gate."""
    probe, ops, remote, t_remote, clock = _sms_probe(tmp_path)
    t_remote.hypervisor = {**t_remote.hypervisor, "handshake_age_sec": None,
                           "ping": {"sent": 10, "received": 0, "max_ms": None}}
    verdict = _sms_gate(probe.collect(now=clock.value), clock.value)
    assert verdict.verdict == "dirty"
    assert {c.name for c in verdict.checks if c.status == "fail"} == {
        "tunnel_handshake_age_sec", "tunnel_ping_lost", "tunnel_ping_max_ms"}


def test_a_cut_always_open_connection_makes_the_s_gate_dirty(tmp_path) -> None:
    """§11 ST4: one of S's five always-open connections down is a dirty gate; reopened 10 minutes ago it still is
    (30-minute settle)."""
    probe, ops, remote, t_remote, clock = _sms_probe(tmp_path)
    ops.supervisor.status["dbsvr01"] = {"alive": False, "opened_at": None, "detail": "exit 255"}
    verdict = _sms_gate(probe.collect(now=clock.value), clock.value)
    assert verdict.verdict == "dirty"
    assert {c.name for c in verdict.checks if c.status == "fail"} == {"mgmt_ssh_down", "mgmt_ssh_settle_sec"}
    assert set(ops.supervisor.status) == {"websvr01", "wassvr01", "wassvr02", "dbsvr01", "loadgen"}
    ops.supervisor.status["dbsvr01"] = {"alive": True, "detail": "",
                                        "opened_at": (clock.value - _timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    verdict = _sms_gate(probe.collect(now=clock.value), clock.value)
    assert {c.name for c in verdict.checks if c.status == "fail"} == {"mgmt_ssh_settle_sec"}


def test_the_s_probe_reads_freshness_of_s_s_own_agents(tmp_path) -> None:
    probe, ops, remote, t_remote, clock = _sms_probe(tmp_path)
    queries = []
    inner = probe.urlopen

    def spy(url, timeout=None):
        queries.append(_urlparse.unquote_plus(url))
        return inner(url, timeout=timeout)

    probe.urlopen = spy
    observations = probe.collect(now=clock.value)
    fresh = [q for q in queries if "sms.cpu.utilization" in q or "sms.server.icmp_status" in q]
    assert fresh and all("10\\.31\\.0\\.11" in q and "192\\.168\\.124" not in q for q in fresh), fresh
    assert not [q for q in queries if "nms.network.snmp_status" in q]
    assert observations["sms_icmp_down"] == 0


def test_the_s_probe_stops_with_why_when_t_s_66_read_is_off(tmp_path) -> None:
    from app.preflight_probe import _NoTunnel

    probe, ops, remote, t_remote, clock = _sms_probe(tmp_path)
    probe.tunnel_ops = _NoTunnel(ops)
    with _pytest.raises(_vm_ops.VmOpsError, match="no 66 read for the tunnel"):
        probe.collect(now=clock.value)


def test_both_vm_testbeds_get_their_probe_and_s_reads_the_tunnel_through_t(tmp_path, monkeypatch) -> None:
    from app import preflight_probe
    from tests.test_vm_ops import sms_inventory_document

    path = tmp_path / "s.json"
    path.write_text(json.dumps(sms_inventory_document()))
    monkeypatch.setattr(_vm_ops, "SMS_INVENTORY", path)
    monkeypatch.setattr(_vm_ops, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(_vm_ops, "_ops", None)
    monkeypatch.setattr(_vm_ops, "_sms_ops", None)
    monkeypatch.setenv("CIVIL_PETITION_SMS_OPS", "1")
    monkeypatch.setenv("CIVIL_PETITION_OPS", "1")
    probes = preflight_probe.vm_domain_probes()
    assert set(probes) == {"civil-petition", "civil-petition-sms"}
    assert probes["civil-petition-sms"].tunnel_ops is probes["civil-petition"].ops
    assert probes["civil-petition-sms"].ops.domain.key == "civil-petition-sms"
    assert probes["civil-petition"].tunnel_ops is probes["civil-petition"].ops
    monkeypatch.setenv("CIVIL_PETITION_OPS", "")
    probes = preflight_probe.vm_domain_probes()
    assert set(probes) == {"civil-petition-sms"} and probes["civil-petition-sms"].tunnel_ops.hypervisor_target == ""
    # An inventory for another domain behind the S switch is refused, not read as S.
    path.write_text(json.dumps({**sms_inventory_document(), "domain": "civil-petition"}))
    monkeypatch.setattr(_vm_ops, "_sms_ops", None)
    with _pytest.raises(_vm_ops.VmOpsError, match="not 'civil-petition-sms'"):
        preflight_probe.vm_domain_probes()


def test_119_load_reads_cpu_without_steal_and_the_highest_load_from_119_s_own_host_metrics() -> None:
    """Q44 answer, task 1: 119's own otelcol hostmetrics (its one target id), CPU busy without idle, iowait and
    steal over the window, the highest 1-minute load in it; no data is an error (the gate holds)."""
    from app.preflight_probe import MONITORING_HOST_TARGET, monitoring_load, monitoring_load_peak

    seen = []

    @contextmanager
    def urlopen(url, timeout=None):
        query = _urlparse.parse_qs(_urlparse.urlparse(url).query)
        seen.append(query)

        class Response:
            def read(self):
                q = query["query"][0]
                value = "23.4" if "load_average" in q else "84.1"
                return json.dumps({"data": {"result": [{"metric": {}, "value": [0, value]}]}}).encode()

        yield Response()

    reading = monitoring_load("http://vm", urlopen=urlopen)
    assert reading == {"cpu_busy_pct": 84.1, "load1_max": 23.4, "window_sec": 600.0}
    cpu, load = seen[0]["query"][0], seen[1]["query"][0]
    assert f'target_id="{MONITORING_HOST_TARGET}"' in cpu and 'state!~"idle|wait|steal"' in cpu and "[600s]" in cpu
    assert load == f'max_over_time(system.cpu.load_average.1m{{target_id="{MONITORING_HOST_TARGET}"}}[600s])'
    start, end = datetime(2026, 11, 25, 12, 30, tzinfo=timezone.utc), datetime(2026, 11, 25, 13, 10, tzinfo=timezone.utc)
    peak = monitoring_load_peak(start, end, "http://vm", urlopen=urlopen)
    assert peak == {"from": "2026-11-25T12:30:00Z", "to": "2026-11-25T13:10:00Z", "cpu_busy_pct_max": 84.1,
                    "load1_max": 23.4}
    assert seen[-2]["query"][0].startswith("max_over_time((100 * sum(rate(") and "[2400s:1m]" in seen[-2]["query"][0]
    assert seen[-1]["time"] == [str(int(end.timestamp()))]

    @contextmanager
    def empty(url, timeout=None):
        class Response:
            def read(self):
                return b'{"data": {"result": []}}'

        yield Response()

    with _pytest.raises(RuntimeError, match="119 host metrics returned no data"):
        monitoring_load("http://vm", urlopen=empty)
