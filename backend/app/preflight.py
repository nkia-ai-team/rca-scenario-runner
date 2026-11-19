"""First-layer preflight gate for the daily scenario cycle.

Contract: spec-scenario-load.md R6 (2026-07-20). Before a scenario is injected,
the runner proves the leading window ``[t1-10m, t1]`` is clean with a set of
*deterministic* checks (this module). The optional second layer (AI adjudication
of borderline cases) is represented here only by the ``AiJudge`` interface; this
module never calls a model itself.

The produced :class:`PreflightVerdict` serialises (``to_meta``) into exactly the
``--preflight-json`` document consumed by ``capture-eval-case.sh`` and recorded
as ``meta.json.preflight`` (spec-eval-data-capture §2.1).
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


Op = Literal["gte", "lte", "gt", "lt", "eq"]
CheckStatus = Literal["pass", "borderline", "fail"]
Verdict = Literal["clean", "clean_after_wait", "ai_judged_clean", "dirty"]
CLEAN_VERDICTS: frozenset[str] = frozenset({"clean", "clean_after_wait", "ai_judged_clean"})


def _compare(value: float, threshold: float, op: Op) -> bool:
    if op == "gte":
        return value >= threshold
    if op == "lte":
        return value <= threshold
    if op == "gt":
        return value > threshold
    if op == "lt":
        return value < threshold
    return value == threshold


class PreflightCheckInput(StrictModel):
    """One deterministic check: compare an observed value to a threshold.

    ``tolerance`` marks a band around the threshold on the *failing* side within
    which the check is ``borderline`` rather than ``fail`` — the only cases the
    second (AI) layer is allowed to adjudicate.
    """

    name: str = Field(min_length=1)
    value: float
    threshold: float
    op: Op
    tolerance: float = Field(default=0.0, ge=0.0)

    def evaluate(self) -> "PreflightCheck":
        if _compare(self.value, self.threshold, self.op):
            status: CheckStatus = "pass"
        elif self.tolerance > 0 and self._within_tolerance():
            status = "borderline"
        else:
            status = "fail"
        return PreflightCheck(
            name=self.name,
            value=self.value,
            threshold=self.threshold,
            op=self.op,
            status=status,
            **{"pass": status == "pass"},
        )

    def _within_tolerance(self) -> bool:
        # Borderline = value fails but is within `tolerance` of the threshold on
        # the failing side.
        if self.op in ("gte", "gt"):
            return self.value >= self.threshold - self.tolerance
        if self.op in ("lte", "lt"):
            return self.value <= self.threshold + self.tolerance
        return abs(self.value - self.threshold) <= self.tolerance


class PreflightCheck(StrictModel):
    name: str
    value: float
    threshold: float
    op: Op
    status: CheckStatus
    # Field name mirrors the JSON contract in capture-eval-case.sh.
    passed: bool = Field(alias="pass")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def to_meta(self) -> dict[str, object]:
        return {
            "name": self.name,
            "value": self.value,
            "threshold": self.threshold,
            "pass": self.passed,
        }


class AiJudgement(StrictModel):
    clean: bool
    rationale: str = Field(min_length=1)


class AiJudge(Protocol):
    """Second-layer interface (spec R6). Implemented outside this module."""

    def adjudicate(
        self, *, window: tuple[str, str], checks: list[PreflightCheck]
    ) -> AiJudgement: ...


class PreflightVerdict(StrictModel):
    window: tuple[str, str]
    verdict: Verdict
    checked_at: str
    waited_sec: int = Field(ge=0)
    checks: list[PreflightCheck]
    ai_judgement: AiJudgement | None = None
    # Firing alarms a decoy deployment of this cycle explains (U7). None when
    # the cycle had no decoy feature; the key then stays out of meta.json.
    excused_alarms: list[dict[str, object]] | None = None

    @property
    def is_clean(self) -> bool:
        return self.verdict in CLEAN_VERDICTS

    def to_meta(self) -> dict[str, object]:
        """Exactly the ``--preflight-json`` document capture-eval-case.sh reads.

        ``excused_alarms`` is added only when the runner had decoys on for the
        cycle (an empty list when nothing was excused or no decoy was drawn), so
        a record from a runner without decoys keeps its exact former shape."""
        document: dict[str, object] = {
            "window": [self.window[0], self.window[1]],
            "verdict": self.verdict,
            "checked_at": self.checked_at,
            "waited_sec": self.waited_sec,
            "checks": [check.to_meta() for check in self.checks],
            "ai_judgement": (
                None
                if self.ai_judgement is None
                else {"clean": self.ai_judgement.clean, "rationale": self.ai_judgement.rationale}
            ),
        }
        if self.excused_alarms is not None:
            document["excused_alarms"] = [dict(item) for item in self.excused_alarms]
        return document


def evaluate_preflight(
    *,
    window: tuple[str, str],
    checks: list[PreflightCheckInput],
    now: datetime,
    waited_sec: int = 0,
    ai_judge: AiJudge | None = None,
    excused_alarms: list[dict[str, object]] | None = None,
) -> PreflightVerdict:
    """Run the deterministic gate and map it to a verdict.

    - any hard ``fail`` -> ``dirty`` (AI never overrides a hard failure);
    - all ``pass`` -> ``clean`` (or ``clean_after_wait`` when we had to wait);
    - otherwise (only borderline checks left) -> ``ai_judged_clean`` if an
      ``ai_judge`` clears it, else ``dirty``.
    """
    if now.tzinfo is None:
        raise ValueError("preflight clock must be timezone-aware")
    if not checks:
        raise ValueError("preflight requires at least one check")
    evaluated = [item.evaluate() for item in checks]
    checked_at = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    statuses = {check.status for check in evaluated}
    ai_judgement: AiJudgement | None = None

    if "fail" in statuses:
        verdict: Verdict = "dirty"
    elif statuses == {"pass"}:
        verdict = "clean_after_wait" if waited_sec > 0 else "clean"
    else:
        # Only borderline (plus possibly pass) remain — the second layer decides.
        if ai_judge is None:
            verdict = "dirty"
        else:
            borderline = [check for check in evaluated if check.status == "borderline"]
            ai_judgement = ai_judge.adjudicate(window=window, checks=borderline)
            verdict = "ai_judged_clean" if ai_judgement.clean else "dirty"

    return PreflightVerdict(
        window=window,
        verdict=verdict,
        checked_at=checked_at,
        waited_sec=waited_sec,
        checks=evaluated,
        ai_judgement=ai_judgement,
        excused_alarms=excused_alarms,
    )


# R6 deterministic first-layer thresholds (spec-scenario-load R6). Values live
# here so the check set is one auditable place; the borderline tolerances mark
# the only band the second (AI) layer may adjudicate.
R6_5XX_NOISE_FLOOR = 0.01
R6_5XX_TOLERANCE = 0.02
R6_ENTRY_P95_SEC = 1.0
R6_ENTRY_P95_TOLERANCE = 0.5

# The observation keys the queue's preflight probe must supply.
R6_REQUIRED_SIGNALS: frozenset[str] = frozenset(
    {
        "baseline_loadgen_alive",
        "achieved_rps",
        "diurnal_rps_floor",
        "user_5xx_rate",
        "entry_p95_sec",
        "active_alarms",
        "open_incidents",
        "prev_pool_pending",
    }
)


# Traditional VM testbed (testbed-services docs/spec-testbed-vm-3tier.md §6-4). The R6 categories are filled
# with this domain's values (its own loadgen on the VM network, the same alarm and incident views), the previous
# run's residue is the executors' armed guards on the six VMs and 66 (there is no commerce pool here), and the
# design adds the tunnel, the monitoring freshness, the router load, the always-open management connections and
# the clock. Each value comes from app.preflight_probe.VmDomainPreflightProbe.
VM_DOMAIN = "civil-petition"
TUNNEL_HANDSHAKE_MAX_SEC = 150.0
TUNNEL_PING_MAX_MS = 20.0
# Mbit/s averaged over the last 10 minutes (§3-6 tunnel row: the 1-minute peak budget of both T and S).
TUNNEL_LOAD_MAX_MBPS = 20.0
SMS_SAMPLE_MAX_AGE_SEC = 90.0
ROUTER_SNMP_MAX_AGE_SEC = 180.0
ROUTER_CPU_MAX_PCT = 50.0
# §6-4 "관리망 도달": a reopened connection leaves a new row in host_connections, so no recording starts within
# 30 minutes of a reopen.
MGMT_SSH_SETTLE_SEC = 1800.0
CLOCK_OFFSET_MAX_MS = 50.0
VM_DOMAIN_REQUIRED_SIGNALS: frozenset[str] = frozenset(
    {
        "baseline_loadgen_alive",
        "achieved_rps",
        "diurnal_rps_floor",
        "user_5xx_rate",
        "entry_p95_sec",
        "active_alarms",
        "open_incidents",
        "residual_guards",
        "tunnel_handshake_age_sec",
        "tunnel_ping_lost",
        "tunnel_ping_max_ms",
        "tunnel_load_mbps",
        "sms_cpu_sample_age_sec",
        "sms_icmp_down",
        "sms_icmp_sample_age_sec",
        "router_snmp_down",
        "router_snmp_sample_age_sec",
        "router_cpu_pct",
        "mgmt_ssh_down",
        "mgmt_ssh_settle_sec",
        "clock_offset_ms",
    }
)
DOMAIN_REQUIRED_SIGNALS: dict[str, frozenset[str]] = {VM_DOMAIN: VM_DOMAIN_REQUIRED_SIGNALS}


def required_signals(domain: str | None = None) -> frozenset[str]:
    """The signal set a domain's preflight must supply; the R6 set for the Kubernetes domains."""
    return DOMAIN_REQUIRED_SIGNALS.get(domain or "", R6_REQUIRED_SIGNALS)


def build_preflight_checks(
    observations: Mapping[str, float], *, domain: str | None = None
) -> list[PreflightCheckInput]:
    """Map the six R6 signal categories to deterministic check inputs.

    Pure: the queue's probe fetches these values from the existing observation
    adapters (loadgen_summary / http_probe / prometheus / database / alarm) and
    hands them here. Raises if any required signal is missing so a partial poll
    fails closed rather than silently passing. ``domain`` selects the domain's
    set (DOMAIN_REQUIRED_SIGNALS); without one it is the R6 set as before.
    """
    required = required_signals(domain)
    missing = required - set(observations)
    if missing:
        raise ValueError(f"preflight is missing required signals: {sorted(missing)}")
    if domain in DOMAIN_REQUIRED_SIGNALS:
        return _vm_domain_checks(observations)
    return [
        # baseline loadgen must be alive (1 = present).
        PreflightCheckInput(
            name="baseline_loadgen_alive",
            value=float(observations["baseline_loadgen_alive"]),
            threshold=1.0,
            op="gte",
        ),
        # achieved baseline RPS within the diurnal band (>= the time-of-day floor).
        PreflightCheckInput(
            name="diurnal_band_rps",
            value=float(observations["achieved_rps"]),
            threshold=float(observations["diurnal_rps_floor"]),
            op="gte",
        ),
        # user-facing 5xx at noise floor (known noise handled by the caller's floor).
        PreflightCheckInput(
            name="user_5xx_rate",
            value=float(observations["user_5xx_rate"]),
            threshold=R6_5XX_NOISE_FLOOR,
            op="lte",
            tolerance=R6_5XX_TOLERANCE,
        ),
        # primary path p95 in the normal band.
        PreflightCheckInput(
            name="entry_p95_sec",
            value=float(observations["entry_p95_sec"]),
            threshold=R6_ENTRY_P95_SEC,
            op="lte",
            tolerance=R6_ENTRY_P95_TOLERANCE,
        ),
        # no active alarms / unresolved incidents. ``active_alarms`` counts the
        # alarms no decoy deployment explains (see app.decoys); with no decoys in
        # the cycle that is every firing alarm, exactly as before.
        PreflightCheckInput(
            name="active_alarms",
            value=float(observations["active_alarms"]),
            threshold=0.0,
            op="eq",
        ),
        PreflightCheckInput(
            name="open_incidents",
            value=float(observations["open_incidents"]),
            threshold=0.0,
            op="eq",
        ),
        # previous scenario residue drained (pool pending == 0, etc.).
        PreflightCheckInput(
            name="prev_pool_pending",
            value=float(observations["prev_pool_pending"]),
            threshold=0.0,
            op="eq",
        ),
    ]


def _vm_domain_checks(observations: Mapping[str, float]) -> list[PreflightCheckInput]:
    def check(name: str, threshold: float, op: Op, *, key: str | None = None, tolerance: float = 0.0) -> PreflightCheckInput:
        return PreflightCheckInput(
            name=name, value=float(observations[key or name]), threshold=threshold, op=op, tolerance=tolerance
        )

    return [
        check("baseline_loadgen_alive", 1.0, "gte"),
        PreflightCheckInput(
            name="diurnal_band_rps",
            value=float(observations["achieved_rps"]),
            threshold=float(observations["diurnal_rps_floor"]),
            op="gte",
        ),
        check("user_5xx_rate", R6_5XX_NOISE_FLOOR, "lte", tolerance=R6_5XX_TOLERANCE),
        check("entry_p95_sec", R6_ENTRY_P95_SEC, "lte", tolerance=R6_ENTRY_P95_TOLERANCE),
        check("active_alarms", 0.0, "eq"),
        check("open_incidents", 0.0, "eq"),
        # The previous run's residue on this domain: armed executor guards on the VMs and 66 (§6-4 "남은 가드").
        check("residual_guards", 0.0, "eq"),
        check("tunnel_handshake_age_sec", TUNNEL_HANDSHAKE_MAX_SEC, "lte"),
        check("tunnel_ping_lost", 0.0, "eq"),
        check("tunnel_ping_max_ms", TUNNEL_PING_MAX_MS, "lte"),
        check("tunnel_load_mbps", TUNNEL_LOAD_MAX_MBPS, "lte"),
        check("sms_cpu_sample_age_sec", SMS_SAMPLE_MAX_AGE_SEC, "lte"),
        check("sms_icmp_down", 0.0, "eq"),
        check("sms_icmp_sample_age_sec", SMS_SAMPLE_MAX_AGE_SEC, "lte"),
        check("router_snmp_down", 0.0, "eq"),
        check("router_snmp_sample_age_sec", ROUTER_SNMP_MAX_AGE_SEC, "lte"),
        check("router_cpu_pct", ROUTER_CPU_MAX_PCT, "lte"),
        check("mgmt_ssh_down", 0.0, "eq"),
        check("mgmt_ssh_settle_sec", MGMT_SSH_SETTLE_SEC, "gte"),
        check("clock_offset_ms", CLOCK_OFFSET_MAX_MS, "lte"),
    ]
