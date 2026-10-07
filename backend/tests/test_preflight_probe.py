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
