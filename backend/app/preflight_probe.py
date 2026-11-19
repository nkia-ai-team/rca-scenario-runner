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
- active alarms: vmalert firing list via the VM select endpoint /api/v1/alerts,
  minus alarms a decoy deployment of the cycle explains (queue-set filter, U7).
- open incidents: the incidents API via the manager session (incident_close;
  web gateway /api/v1/ai/incidents by default, status=active, every page).
- pool residue: max db.client.connections.pending_requests over commerce apps.

Any fetch failure raises — the queue's gate treats a probe failure as pause,
never as silently clean.
"""
from __future__ import annotations

import json
import re
import os
import shlex
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

from app import vm_ops as _vm_ops

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
        # Set by the queue for one collect() when the cycle ran decoy deployments
        # (U7): splits firing alerts into (still blocking, explained by a decoy).
        self.alarm_filter = None
        self.last_excused_alarms: list[dict] = []

    def set_alarm_filter(self, alarm_filter) -> None:
        self.alarm_filter = alarm_filter

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
        firing = [item for item in alerts if str(item.get("state", "firing")) != "inactive"]
        self.last_excused_alarms = []
        if self.alarm_filter is None:
            return len(firing)
        remaining, excused = self.alarm_filter(firing)
        self.last_excused_alarms = list(excused)
        return len(remaining)


# ---------------------------------------------------------------------------------------------------------------
# Traditional VM testbed (civil-petition): testbed-services docs/spec-testbed-vm-3tier.md §6-4.
#
# Where each value comes from:
# - loadgen liveness, achieved rate, abnormal share, entry p95: the domain's own loadgen VM, read now over the
#   always-open management connection (app.vm_ops.read_loadgen_live). The loadgen sends nothing to 119 and is not
#   an SMS target, so reading it per preflight leaves nothing in a recording.
# - the time-of-day floor: half the loadgen's own target rate for this KST hour and the previous one (the hour
#   table of civil-petition/loadgen/entrypoint.sh with the PEAK_RPS and TROUGH_RPS the unit runs with). A dead or
#   degenerate generator falls far below it; a healthy one sits at its target.
# - alarms, incidents and the pool residue: the same global views as the other domains (ProductionPreflightProbe).
# - tunnel: 66's helper (`tunnel`, `guards`, `version`), read now (handshake age, 10 pings to the monitoring end) plus the WireGuard byte
#   counters of the periodic reads for the 10-minute average.
# - monitoring freshness: 119 VictoriaMetrics (sms.cpu.utilization, sms.server.icmp_status by the agents' host_name,
#   nms.network.snmp_status of the router's target from the target table).
# - router CPU, guards, clock: the periodic read (app.vm_ops), two router /proc/stat lines about 5 minutes apart.
# - management connections: the ControlMaster supervisor, six VMs and 66 (down count, time since the last reopen).
# Anything missing or stale raises with the remote's own words, so the gate waits and then pauses with the reason
# (U3) instead of judging on a guess.
# ---------------------------------------------------------------------------------------------------------------
KST = ZoneInfo("Asia/Seoul")
# civil-petition/loadgen/entrypoint.sh diurnal_pct (percent of the peak-trough span per KST hour).
VM_DOMAIN_DIURNAL_PCT = (25, 15, 7, 2, 0, 2, 7, 15, 25, 37, 50, 63, 75, 85, 93, 98, 100, 98, 93, 85, 75, 63, 50, 37)
VM_DOMAIN_FLOOR_SHARE = 0.5
TUNNEL_NEVER_SEC = 1_000_000.0
TUNNEL_PING_COUNT = 10
TUNNEL_LOAD_WINDOW_SEC = 600
TUNNEL_LOAD_WINDOW_MAX_SEC = 900
ROUTER_CPU_WINDOW_MIN_SEC = 240
ROUTER_CPU_WINDOW_MAX_SEC = 660
FRESHNESS_LOOKBACK_SEC = 900
VM_DOMAIN_TARGETS_PATH = Path(
    os.environ.get("CIVIL_PETITION_TARGETS_PATH", "/opt/lucida/civil-petition-deploy/inventory/t.targets.json")
)


def vm_domain_target_rps(hour: int, env_line: str) -> int:
    """The loadgen's target arrival rate for a KST hour, with the integer arithmetic of entrypoint.sh."""
    values: dict[str, str] = {}
    for item in shlex.split(env_line or ""):
        name, _, value = item.partition("=")
        values[name] = value
    try:
        peak, trough = int(values["PEAK_RPS"]), int(values["TROUGH_RPS"])
    except (KeyError, ValueError):
        raise _vm_ops.VmOpsError(f"loadgen unit environment lacks PEAK_RPS/TROUGH_RPS: {env_line[:200]!r}") from None
    return trough + ((peak - trough) * VM_DOMAIN_DIURNAL_PCT[hour % 24] + 50) // 100


def vm_domain_rps_floor(now: datetime, env_line: str) -> float:
    hour = now.astimezone(KST).hour
    target = min(vm_domain_target_rps(hour, env_line), vm_domain_target_rps(hour - 1, env_line))
    return VM_DOMAIN_FLOOR_SHARE * target


def router_target_id(path: Path = VM_DOMAIN_TARGETS_PATH) -> str:
    """The router's NMS target id from the target table (deploy/inventory/t.targets.json, remade after 119 patches)."""
    try:
        table = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        raise _vm_ops.VmOpsError(f"target table unreadable at {path}: {type(error).__name__}: {error}") from None
    for device in table.get("network_devices") or []:
        if device.get("name") == _vm_ops.ROUTER_NAME or device.get("address") == _vm_ops.ROUTER_POLL_ADDRESS:
            target = device.get("target_id")
            if isinstance(target, str) and re.fullmatch(r"[0-9a-f-]{36}", target):
                return target
    raise _vm_ops.VmOpsError(f"no router target in {path}")


class VmDomainPreflightProbe:
    """§6-4 signals for the VM testbed. Same interface as ProductionPreflightProbe."""

    domain = "civil-petition"

    def __init__(
        self,
        *,
        ops: "_vm_ops.VmOps",
        base: ProductionPreflightProbe | None = None,
        vm_url: str | None = None,
        urlopen=None,
        incident_count=None,
        targets_path: Path | None = None,
    ) -> None:
        self.ops = ops
        self.base = base or ProductionPreflightProbe(vm_url=vm_url, urlopen=urlopen)
        self.vm_url = (vm_url or self.base.vm_url).rstrip("/")
        self.urlopen = urlopen or self.base.urlopen
        self.incident_count = incident_count or open_incident_count
        self.targets_path = targets_path or VM_DOMAIN_TARGETS_PATH

    def set_alarm_filter(self, alarm_filter) -> None:
        self.base.set_alarm_filter(alarm_filter)

    @property
    def last_excused_alarms(self) -> list[dict]:
        return self.base.last_excused_alarms

    def collect(self, *, now: datetime) -> Mapping[str, float]:
        alive, document, env_line = self.ops.read_loadgen_live()
        if document.get("domain") != self.domain or "scenario_id" in document:
            raise _vm_ops.VmOpsError("loadgen live summary is not this domain's baseline document")
        age = (now - datetime.fromisoformat(str(document["observed_at"]).replace("Z", "+00:00"))).total_seconds()
        if not -10 <= age <= 30:
            raise _vm_ops.VmOpsError(f"loadgen live summary is {age:.0f}s old (limit 30s)")
        if "http_abnormal_rate" not in document or "http_p95_ms" not in document:
            raise _vm_ops.VmOpsError(
                f"loadgen live summary has no requests in its window (http_count={document.get('http_count')})"
            )
        facts = self._fresh_facts(now)
        hypervisor = self.ops.hypervisor_facts()
        handshake = hypervisor.get("handshake_age_sec")
        ping = hypervisor.get("ping") or {}
        sent, received = int(ping.get("sent", 0)), int(ping.get("received", 0))
        if sent < TUNNEL_PING_COUNT:
            raise _vm_ops.VmOpsError(f"hypervisor sent {sent} pings, expected {TUNNEL_PING_COUNT}")
        max_ms = ping.get("max_ms")
        # The six VMs and 66 (T4S contract 6: the 66 executors refuse to run without its master, and a reopen there
        # leaves session lines 66's filter cannot name, so it gets the same 30-minute settle).
        masters = self.ops.supervisor.snapshot()
        expected = {host.name for host in self.ops.supervisor.hosts}
        missing = expected - set(masters)
        if missing:
            raise _vm_ops.VmOpsError(f"no master status for {sorted(missing)}")
        down = sum(1 for status in masters.values() if not status.get("alive"))
        settle = 0.0
        if not down:
            opened = [datetime.fromisoformat(str(status["opened_at"]).replace("Z", "+00:00")) for status in masters.values()]
            settle = max(0.0, min((now - at).total_seconds() for at in opened))
        guards = sum(max(int(f["guards_armed"]), int(f["guard_timers"])) for f in facts.values())
        guards += int(hypervisor.get("guards_active", 0))
        freshness = self._freshness()
        return {
            "baseline_loadgen_alive": 1.0 if alive else 0.0,
            "achieved_rps": float(document.get("achieved_rps", 0.0)),
            "diurnal_rps_floor": vm_domain_rps_floor(now, env_line),
            "user_5xx_rate": float(document["http_abnormal_rate"]),
            "entry_p95_sec": float(document["http_p95_ms"]) / 1000.0,
            "active_alarms": float(self.base._firing_alarm_count()),
            "open_incidents": float(self.incident_count()),
            # The same global residue signal the other domains are held on (U7 rule as is, review rv-t4r3 L1).
            "prev_pool_pending": self.base._vm_scalar(POOL_PENDING_QUERY, default=0.0),
            "residual_guards": float(guards),
            # None or negative (the helper's -1) = no handshake at all (review rv-t4r3 M1).
            "tunnel_handshake_age_sec": (
                float(handshake) if handshake is not None and float(handshake) >= 0 else TUNNEL_NEVER_SEC
            ),
            "tunnel_ping_lost": float(sent - received),
            "tunnel_ping_max_ms": float(max_ms) if max_ms is not None else TUNNEL_NEVER_SEC,
            "tunnel_load_mbps": self._tunnel_load_mbps(),
            **freshness,
            "router_cpu_pct": self._router_cpu_pct(),
            "mgmt_ssh_down": float(down),
            "mgmt_ssh_settle_sec": settle,
            "clock_offset_ms": max(_vm_ops.chrony_offset_ms(f["chrony"]) for f in facts.values()),
        }

    def _fresh_facts(self, now: datetime) -> dict[str, dict]:
        document = self.ops.load()
        facts: dict[str, dict] = {}
        problems: list[str] = []
        for host in self.ops.hosts:
            entry = document["hosts"].get(host.name) or {}
            current = entry.get("facts")
            error = entry.get("last_error")
            if current is None:
                problems.append(f"{host.name}: never read" + (f" ({error['detail']})" if error else ""))
                continue
            age = (now - datetime.fromisoformat(current["read_at"].replace("Z", "+00:00"))).total_seconds()
            if age > _vm_ops.FACTS_MAX_AGE_SEC:
                problems.append(
                    f"{host.name}: last read {int(age)}s ago (limit {_vm_ops.FACTS_MAX_AGE_SEC}s)"
                    + (f", last error {error['detail']}" if error else "")
                )
                continue
            facts[host.name] = current
        if problems:
            raise _vm_ops.VmOpsError("periodic management read is stale: " + "; ".join(problems))
        return facts

    def _tunnel_load_mbps(self) -> float:
        history = self.ops.load()["hypervisor"].get("history") or []
        if not history:
            raise _vm_ops.VmOpsError("no tunnel counter samples")
        latest = history[-1]
        latest_at = datetime.fromisoformat(latest["at"].replace("Z", "+00:00"))
        for sample in reversed(history[:-1]):
            span = (latest_at - datetime.fromisoformat(sample["at"].replace("Z", "+00:00"))).total_seconds()
            if span < TUNNEL_LOAD_WINDOW_SEC:
                continue
            if span > TUNNEL_LOAD_WINDOW_MAX_SEC:
                break
            moved = (latest["wg_rx_bytes"] + latest["wg_tx_bytes"]) - (sample["wg_rx_bytes"] + sample["wg_tx_bytes"])
            if moved < 0:
                raise _vm_ops.VmOpsError("tunnel byte counters went backwards (wg0 was recreated); wait for a new window")
            return moved * 8 / span / 1_000_000
        raise _vm_ops.VmOpsError(
            f"no tunnel counter sample {TUNNEL_LOAD_WINDOW_SEC}-{TUNNEL_LOAD_WINDOW_MAX_SEC}s before the latest"
        )

    def _router_cpu_pct(self) -> float:
        entry = self.ops.load()["hosts"].get(_vm_ops.ROUTER_NAME) or {}
        history = entry.get("cpu_history") or []
        if len(history) < 2:
            raise _vm_ops.VmOpsError("router CPU needs two periodic reads")
        older, newer = history[-2], history[-1]
        span = (
            datetime.fromisoformat(newer["at"].replace("Z", "+00:00"))
            - datetime.fromisoformat(older["at"].replace("Z", "+00:00"))
        ).total_seconds()
        if not ROUTER_CPU_WINDOW_MIN_SEC <= span <= ROUTER_CPU_WINDOW_MAX_SEC:
            raise _vm_ops.VmOpsError(f"router CPU samples are {int(span)}s apart, expected about 300s")
        return _vm_ops.cpu_busy_percent(older["procstat"], newer["procstat"])

    def _vm_vector(self, promql: str) -> list[dict]:
        query = urllib.parse.urlencode({"query": promql})
        with self.urlopen(f"{self.vm_url}/api/v1/query?{query}", timeout=_TIMEOUT_SEC) as response:
            document = json.loads(response.read())
        if document.get("status") not in (None, "success"):
            raise RuntimeError(f"VM query failed: {document.get('error', document)}")
        return document.get("data", {}).get("result", [])

    def _by_host(self, promql: str) -> dict[str, float]:
        return {row["metric"].get("host_name", ""): float(row["value"][1]) for row in self._vm_vector(promql)}

    def _freshness(self) -> dict[str, float]:
        hosts = sorted(_vm_ops.SERVER_AGENT_HOSTS.values())
        selector = "|".join(re.escape(host) for host in hosts)
        lookback = f"[{FRESHNESS_LOOKBACK_SEC}s]"
        missing_age = float(FRESHNESS_LOOKBACK_SEC + 1)

        def ages(name: str) -> dict[str, float]:
            return self._by_host(
                f'time() - max by (host_name) (tlast_over_time({{__name__="{name}",host_name=~"{selector}"}}{lookback}))'
            )

        cpu_ages = ages("sms.cpu.utilization")
        icmp_ages = ages("sms.server.icmp_status")
        icmp_last = self._by_host(
            f'max by (host_name) (last_over_time({{__name__="sms.server.icmp_status",host_name=~"{selector}"}}{lookback}))'
        )
        router = router_target_id(self.targets_path)
        router_age = self._vm_vector(
            f'time() - max(tlast_over_time({{__name__="nms.network.snmp_status",target_id="{router}"}}{lookback}))'
        )
        router_last = self._vm_vector(
            f'max(last_over_time({{__name__="nms.network.snmp_status",target_id="{router}"}}{lookback}))'
        )
        return {
            "sms_cpu_sample_age_sec": max(cpu_ages.get(host, missing_age) for host in hosts),
            "sms_icmp_down": float(sum(1 for host in hosts if icmp_last.get(host) != 1.0)),
            "sms_icmp_sample_age_sec": max(icmp_ages.get(host, missing_age) for host in hosts),
            "router_snmp_down": 0.0 if router_last and float(router_last[0]["value"][1]) == 1.0 else 1.0,
            "router_snmp_sample_age_sec": float(router_age[0]["value"][1]) if router_age else missing_age,
        }


def vm_domain_probes() -> dict[str, VmDomainPreflightProbe]:
    """Domain -> preflight probe for the VM testbed when its operations are on (CIVIL_PETITION_OPS)."""
    ops = _vm_ops.get_vm_ops()
    if ops is None:
        return {}
    return {VmDomainPreflightProbe.domain: VmDomainPreflightProbe(ops=ops)}
