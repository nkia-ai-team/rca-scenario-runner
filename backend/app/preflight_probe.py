"""Production R6 preflight probe — the eight deterministic first-layer signals.

Signal sources were fixed empirically (2026-07-21, 119 live):
- baseline loadgen liveness/rate: tb-runner systemd unit + k6 journal
  ("N.NN iters/s" progress lines; the APM `rps` metric is NOT requests/sec —
  measured 0.083 while k6 delivered 4.0 iters/s, so it is unusable here).
- entry p95: apm.agent.otel.java.percentile95{commerce-gateway} in **ms** (87.47
  measured at baseline) -> /1000.
- user 5xx: counted from otel_traces_local SERVER spans (2026-07-30). It used to
  read apm.agent.otel.java.error_rate, but that rollup discards all but one
  insert block per service-minute, so the gateway ratio came from ~5 of the ~105
  requests actually served — one sampled failure read as 20% against a 1% floor.
- active alarms: vmalert firing list via the VM select endpoint /api/v1/alerts.
- open incidents: observer 18087 via the manager session (incident_close).
- pool residue: max db.client.connections.pending_requests over commerce apps.

Any fetch failure raises — the queue's gate treats a probe failure as pause,
never as silently clean.
"""
from __future__ import annotations

import json
import re
import os
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Mapping

from app.incident_close import open_incident_count
# Single source for the trace endpoint and its credentials: a second copy of an
# allowlist or an endpoint is how F06-P's only success condition drifted away
# from the canonical one and failed every tick.
from app.live_probes import (
    CLICKHOUSE_PASSWORD,
    CLICKHOUSE_TEMPLATES,
    CLICKHOUSE_URL,
    CLICKHOUSE_USER,
)

DEFAULT_VM_URL = "http://192.168.230.119:18428"
LOADGEN_KEY = "/root/.ssh/tb_key"
LOADGEN_TARGET = "nkia@192.168.122.206"
# Baseline unit currently drives a flat 4 iters/s journey rate; the floor only
# has to catch a dead or degenerate generator, not model the diurnal curve.
DIURNAL_RPS_FLOOR = 1.0
_TIMEOUT_SEC = 10

P95_QUERY = 'max(apm.agent.otel.java.percentile95{service_name="commerce-gateway"}) / 1000'
POOL_PENDING_QUERY = 'max(db.client.connections.pending_requests{service_name=~"commerce-.*"})'


class ProductionPreflightProbe:
    def __init__(self, *, vm_url: str | None = None, process_runner=subprocess.run, urlopen=None):
        self.vm_url = (vm_url or os.environ.get("PREFLIGHT_VM_URL") or DEFAULT_VM_URL).rstrip("/")
        self.process_runner = process_runner
        self.urlopen = urlopen or urllib.request.urlopen

    def collect(self, *, now: datetime) -> Mapping[str, float]:
        del now
        alive, iters_per_sec = self._loadgen_state()
        return {
            "baseline_loadgen_alive": 1.0 if alive else 0.0,
            "achieved_rps": iters_per_sec,
            "diurnal_rps_floor": DIURNAL_RPS_FLOOR,
            "user_5xx_rate": self._gateway_5xx_fraction(),
            "entry_p95_sec": self._vm_scalar(P95_QUERY),
            "active_alarms": float(self._firing_alarm_count()),
            "open_incidents": float(open_incident_count()),
            "prev_pool_pending": self._vm_scalar(POOL_PENDING_QUERY, default=0.0),
        }

    def _loadgen_state(self) -> tuple[bool, float]:
        result = self.process_runner(
            [
                "ssh", "-i", LOADGEN_KEY, "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10",
                LOADGEN_TARGET,
                "systemctl is-active loadgen-commerce; "
                "sudo journalctl -u loadgen-commerce -n 20 --no-pager",
            ],
            check=True, capture_output=True, text=True, timeout=20,
        )
        lines = result.stdout.splitlines()
        alive = bool(lines) and lines[0].strip() == "active"
        rates = re.findall(r"([0-9]+(?:\.[0-9]+)?) iters/s", result.stdout)
        return alive, float(rates[-1]) if rates else 0.0

    def _gateway_5xx_fraction(self) -> float:
        """Entry-point 5xx share, counted from traces rather than the APM rollup.

        The rollup keeps roughly one insert block per service-minute, so at the
        gateway it surfaced ~5 requests of the ~105 actually served. A single
        sampled failure then reads as 20% against a 1% floor and the gate goes
        dirty on a healthy system, while real errors outside the surviving block
        are invisible. Same defect, same fix as clickhouse.service_error_rate.
        """
        sql = CLICKHOUSE_TEMPLATES["trace-service-error-rate-v1"] % "commerce-gateway"
        request = urllib.request.Request(
            CLICKHOUSE_URL,
            data=sql.encode("utf-8"),
            headers={
                "X-ClickHouse-User": CLICKHOUSE_USER,
                "X-ClickHouse-Key": CLICKHOUSE_PASSWORD,
            },
            method="POST",
        )
        with self.urlopen(request, timeout=_TIMEOUT_SEC) as response:
            document = json.loads(response.read())
        # Percent on the wire; the R6 thresholds are fractions.
        return float(document["data"][0]["value"]) / 100

    def _vm_scalar(self, promql: str, *, default: float | None = None) -> float:
        query = urllib.parse.urlencode({"query": promql})
        with self.urlopen(f"{self.vm_url}/api/v1/query?{query}", timeout=_TIMEOUT_SEC) as response:
            document = json.loads(response.read())
        rows = document.get("data", {}).get("result", [])
        if not rows:
            if default is None:
                raise RuntimeError(f"preflight VM query returned no data: {promql}")
            return default
        return float(rows[0]["value"][1])

    def _firing_alarm_count(self) -> int:
        with self.urlopen(f"{self.vm_url}/api/v1/alerts", timeout=_TIMEOUT_SEC) as response:
            document = json.loads(response.read())
        alerts = document.get("data", {}).get("alerts", [])
        return sum(1 for item in alerts if str(item.get("state", "firing")) != "inactive")
