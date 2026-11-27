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
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
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


# 119 itself (questions.md Q44 answer, task 1): when 119 is pushed by other load on its host, its pollers fall behind
# and write false DOWNs into a recording (11-25 21:57:59 the router read DOWN for 44 s with 119 at CPU 84.7 %, load
# 15.5 ~ 23.4 on 8 vCPUs). 119's own host metrics (otelcol hostmetrics, the one target id below) answer it.
MONITORING_HOST_TARGET = "00000000-0000-0000-0000-00000000aaaa"
# CPU busy share as user + system (review-session thresholds, board 11-27 12:1x), and steal: the share the host
# (104) takes away from 119, the real cause of the false DOWNs.
MONITORING_CPU_BUSY = (
    '100 * sum(rate(system.cpu.time{{target_id="{t}",state=~"user|system"}}[{w}]))'
    ' / sum(rate(system.cpu.time{{target_id="{t}"}}[{w}]))'
)
MONITORING_CPU_STEAL = (
    '100 * sum(rate(system.cpu.time{{target_id="{t}",state="steal"}}[{w}]))'
    ' / sum(rate(system.cpu.time{{target_id="{t}"}}[{w}]))'
)
MONITORING_LOAD1 = 'system.cpu.load_average.1m{{target_id="{t}"}}'


def monitoring_load(vm_url: str | None = None, *, urlopen=None, now: datetime | None = None,
                    window_sec: int = 600) -> dict[str, float]:
    """119 over the last ``window_sec``, read from 119's VictoriaMetrics: CPU user + system and steal percent averaged
    over it, and the lowest 1-minute load average in it (a load that stayed up the whole window). Raises when one
    is missing (the gate then holds)."""
    base = (vm_url or os.environ.get("PREFLIGHT_VM_URL") or DEFAULT_VM_URL).rstrip("/")
    opener = urlopen or urllib.request.urlopen
    window = f"{int(window_sec)}s"

    def scalar(promql: str) -> float:
        params = {"query": promql}
        if now is not None:
            params["time"] = str(int(now.timestamp()))
        with opener(f"{base}/api/v1/query?{urllib.parse.urlencode(params)}", timeout=_TIMEOUT_SEC) as response:
            document = json.loads(response.read())
        rows = document.get("data", {}).get("result", [])
        if not rows:
            raise RuntimeError(f"119 host metrics returned no data: {promql}")
        return float(rows[0]["value"][1])

    return {
        "cpu_busy_pct": scalar(MONITORING_CPU_BUSY.format(t=MONITORING_HOST_TARGET, w=window)),
        "steal_pct": scalar(MONITORING_CPU_STEAL.format(t=MONITORING_HOST_TARGET, w=window)),
        "load1_min": scalar(f"min_over_time({MONITORING_LOAD1.format(t=MONITORING_HOST_TARGET)}[{window}])"),
        "window_sec": float(window_sec),
    }


def monitoring_load_peak(start: datetime, end: datetime, vm_url: str | None = None, *, urlopen=None) -> dict[str, Any]:
    """The highest 1-minute CPU user + system percent, steal percent and 1-minute load average of 119 between
    ``start`` and ``end`` (a recording window), for the record of that recording."""
    base = (vm_url or os.environ.get("PREFLIGHT_VM_URL") or DEFAULT_VM_URL).rstrip("/")
    opener = urlopen or urllib.request.urlopen
    span = max(60, int((end - start).total_seconds()))
    cpu = MONITORING_CPU_BUSY.format(t=MONITORING_HOST_TARGET, w="1m")
    steal = MONITORING_CPU_STEAL.format(t=MONITORING_HOST_TARGET, w="1m")
    load = MONITORING_LOAD1.format(t=MONITORING_HOST_TARGET)
    out: dict[str, Any] = {
        "from": start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    for key, promql in (("cpu_busy_pct_max", f"max_over_time(({cpu})[{span}s:1m])"),
                        ("steal_pct_max", f"max_over_time(({steal})[{span}s:1m])"),
                        ("load1_max", f"max_over_time({load}[{span}s])")):
        params = {"query": promql, "time": str(int(end.timestamp()))}
        with opener(f"{base}/api/v1/query?{urllib.parse.urlencode(params)}", timeout=_TIMEOUT_SEC) as response:
            rows = json.loads(response.read()).get("data", {}).get("result", [])
        out[key] = float(rows[0]["value"][1]) if rows else None
    return out


def promql_regex_literal(values) -> str:
    r"""An alternation of exact values for a MetricsQL `=~"..."` matcher. The regex escape (``\.``) must itself be
    escaped inside the double-quoted string literal (``\\.``): VictoriaMetrics rejects ``"192\.168..."`` with 422
    «cannot parse string literal ... invalid syntax» (PF422, the S and T freshness queries; the same fix as
    testbed-services 4fccfc6). Quotes are escaped too."""
    return "|".join(re.escape(str(value)) for value in values).replace("\\", "\\\\").replace('"', '\\"')


class PreflightSignalError(RuntimeError):
    """A preflight signal could not be read; the message names the signal and the request (PF422)."""


def describe_error(error: BaseException) -> str:
    """What failed, for the pause reason and the log: an HTTP error says its status, the path it went to (with the
    PromQL of a VictoriaMetrics query) and the start of the answer body (VictoriaMetrics puts the reason there)."""
    if isinstance(error, urllib.error.HTTPError):
        url = urllib.parse.urlparse(getattr(error, "url", None) or error.filename or "")
        where = url.path or "?"
        promql = urllib.parse.parse_qs(url.query).get("query")
        if promql:
            where += f" query={promql[0][:160]}"
        try:
            body = error.read()[:240].decode("utf-8", "replace").strip()
        except Exception:
            body = ""
        return f"HTTP {error.code} {error.reason} from {url.netloc}{where}" + (f": {body}" if body else "")
    if isinstance(error, urllib.error.URLError):
        return f"{type(error).__name__}: {error.reason}"
    return f"{type(error).__name__}: {error}"


def read_signal(name: str, read):
    """``read()`` with its failure named by the signal and the request (PF422: a bare «HTTP Error 422» in the pause
    reason did not say which of the preflight's requests had failed)."""
    try:
        return read()
    except PreflightSignalError:
        raise
    except Exception as error:
        raise PreflightSignalError(f"{name}: {describe_error(error)}"[:600]) from error


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
        alive, iters_per_sec = read_signal("baseline_loadgen", self._loadgen_state)
        return {
            "baseline_loadgen_alive": 1.0 if alive else 0.0,
            "achieved_rps": iters_per_sec,
            "diurnal_rps_floor": DIURNAL_RPS_FLOOR,
            "user_5xx_rate": read_signal("user_5xx_rate", self._gateway_5xx_fraction),
            "entry_p95_sec": read_signal("entry_p95_sec", lambda: self._vm_scalar(P95_QUERY)),
            "active_alarms": float(read_signal("active_alarms", self._firing_alarm_count)),
            "open_incidents": float(read_signal("open_incidents", open_incident_count)),
            "prev_pool_pending": read_signal(
                "prev_pool_pending", lambda: self._vm_scalar(POOL_PENDING_QUERY, default=0.0)
            ),
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
# - routine operator session: seconds since the last successful one (app.vm_ops.OperatorRoutine, Q35 condition ②).
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
    """§6-4 signals for a VM testbed (T, and S with S design §7-4). Same interface as ProductionPreflightProbe.

    The domain comes from its VmOps. A testbed without a router (S) has no router rows. A testbed without 66
    executors (S) reads the tunnel through ``tunnel_ops`` (T's VmOps: the same 66 end, the same helper) and counts
    only its own VMs' guards."""

    def __init__(
        self,
        *,
        ops: "_vm_ops.VmOps",
        base: ProductionPreflightProbe | None = None,
        vm_url: str | None = None,
        urlopen=None,
        incident_count=None,
        targets_path: Path | None = None,
        tunnel_ops: "_vm_ops.VmOps | None" = None,
    ) -> None:
        self.ops = ops
        self.domain = ops.domain.key
        self.tunnel_ops = tunnel_ops if tunnel_ops is not None else ops
        self.base = base or ProductionPreflightProbe(vm_url=vm_url, urlopen=urlopen)
        self.vm_url = (vm_url or self.base.vm_url).rstrip("/")
        self.urlopen = urlopen or self.base.urlopen
        self.incident_count = incident_count or open_incident_count
        self.targets_path = targets_path or VM_DOMAIN_TARGETS_PATH
        # decoys.held_excuses of the cycle while the queue evaluates (set_held_decoys): the executor guards that VM
        # decoys held through the recording (D1 (b)) keep on their nodes are no residue.
        self.held: dict = {}

    def set_alarm_filter(self, alarm_filter) -> None:
        self.base.set_alarm_filter(alarm_filter)

    def set_held_decoys(self, held) -> None:
        self.held = dict(held or {})

    @property
    def last_excused_alarms(self) -> list[dict]:
        return self.base.last_excused_alarms

    def collect(self, *, now: datetime) -> Mapping[str, float]:
        alive, document, env_line = read_signal("baseline_loadgen", self.ops.read_loadgen_live)
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
        if not self.tunnel_ops.hypervisor_target:
            raise _vm_ops.VmOpsError(f"{self.domain}: no 66 read for the tunnel (the T testbed's operations are off)")
        hypervisor = read_signal("tunnel", self.tunnel_ops.hypervisor_facts)
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
        # One guard per held decoy is excused on its own node only; never below zero, never moved to another node.
        guards = sum(
            max(0, max(int(f["guards_armed"]), int(f["guard_timers"])) - int((self.held.get(host) or {}).get("guards", 0)))
            for host, f in facts.items()
        )
        if self.tunnel_ops is self.ops:
            # 66's guards belong to this domain's 66 executors (T); S has none there.
            guards += int(hypervisor.get("guards_active", 0))
        freshness = read_signal("monitoring_freshness", self._freshness)
        tunnel_load = self._tunnel_load_mbps()
        router = {"router_cpu_pct": self._router_cpu_pct()} if self.ops.domain.router else {}
        return {
            "baseline_loadgen_alive": 1.0 if alive else 0.0,
            "achieved_rps": float(document.get("achieved_rps", 0.0)),
            "diurnal_rps_floor": vm_domain_rps_floor(now, env_line),
            "user_5xx_rate": float(document["http_abnormal_rate"]),
            "entry_p95_sec": float(document["http_p95_ms"]) / 1000.0,
            "active_alarms": float(read_signal("active_alarms", self.base._firing_alarm_count)),
            "open_incidents": float(read_signal("open_incidents", self.incident_count)),
            # The same global residue signal the other domains are held on (U7 rule as is, review rv-t4r3 L1).
            "prev_pool_pending": read_signal(
                "prev_pool_pending", lambda: self.base._vm_scalar(POOL_PENDING_QUERY, default=0.0)
            ),
            "residual_guards": float(guards),
            # None or negative (the helper's -1) = no handshake at all (review rv-t4r3 M1).
            "tunnel_handshake_age_sec": (
                float(handshake) if handshake is not None and float(handshake) >= 0 else TUNNEL_NEVER_SEC
            ),
            "tunnel_ping_lost": float(sent - received),
            "tunnel_ping_max_ms": float(max_ms) if max_ms is not None else TUNNEL_NEVER_SEC,
            "tunnel_load_mbps": tunnel_load,
            **freshness,
            **router,
            "mgmt_ssh_down": float(down),
            "mgmt_ssh_settle_sec": settle,
            "clock_offset_ms": max(_vm_ops.chrony_offset_ms(f["chrony"]) for f in facts.values()),
            # Q35 condition ②: the routine operator session keeps running (any VM).
            "operator_routine_age_sec": self.ops.routine.last_ok_age_sec(now),
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
        history = self.tunnel_ops.load()["hypervisor"].get("history") or []
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
        hosts = sorted(self.ops.domain.agent_hosts.values())
        selector = promql_regex_literal(hosts)
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
        servers = {
            "sms_cpu_sample_age_sec": max(cpu_ages.get(host, missing_age) for host in hosts),
            "sms_icmp_down": float(sum(1 for host in hosts if icmp_last.get(host) != 1.0)),
            "sms_icmp_sample_age_sec": max(icmp_ages.get(host, missing_age) for host in hosts),
        }
        if not self.ops.domain.router:
            return servers
        router = router_target_id(self.targets_path)
        router_age = self._vm_vector(
            f'time() - max(tlast_over_time({{__name__="nms.network.snmp_status",target_id="{router}"}}{lookback}))'
        )
        router_last = self._vm_vector(
            f'max(last_over_time({{__name__="nms.network.snmp_status",target_id="{router}"}}{lookback}))'
        )
        return {
            **servers,
            "router_snmp_down": 0.0 if router_last and float(router_last[0]["value"][1]) == 1.0 else 1.0,
            "router_snmp_sample_age_sec": float(router_age[0]["value"][1]) if router_age else missing_age,
        }


def vm_domain_probes() -> dict[str, VmDomainPreflightProbe]:
    """Domain -> preflight probe for each VM testbed whose operations are on (CIVIL_PETITION_OPS for T,
    CIVIL_PETITION_SMS_OPS for S). S reads the tunnel through T's 66 read; with T off its probe raises with why."""
    probes: dict[str, VmDomainPreflightProbe] = {}
    ops = _vm_ops.get_vm_ops()
    if ops is not None:
        probes[ops.domain.key] = VmDomainPreflightProbe(ops=ops)
    sms = _vm_ops.get_sms_ops()
    if sms is not None:
        probes[sms.domain.key] = VmDomainPreflightProbe(
            ops=sms, tunnel_ops=ops if ops is not None else _NoTunnel(sms)
        )
    return probes


class _NoTunnel:
    """Stands in for T's VmOps when only S is on: no 66 read, so the S preflight stops with why."""

    hypervisor_target = ""

    def __init__(self, ops: "_vm_ops.VmOps") -> None:
        self.domain = ops.domain
