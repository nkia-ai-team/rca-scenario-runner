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

    @property
    def is_clean(self) -> bool:
        return self.verdict in CLEAN_VERDICTS

    def to_meta(self) -> dict[str, object]:
        """Exactly the ``--preflight-json`` document capture-eval-case.sh reads."""
        return {
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


def evaluate_preflight(
    *,
    window: tuple[str, str],
    checks: list[PreflightCheckInput],
    now: datetime,
    waited_sec: int = 0,
    ai_judge: AiJudge | None = None,
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


def build_preflight_checks(observations: Mapping[str, float]) -> list[PreflightCheckInput]:
    """Map the six R6 signal categories to deterministic check inputs.

    Pure: the queue's probe fetches these values from the existing observation
    adapters (loadgen_summary / http_probe / prometheus / database / alarm) and
    hands them here. Raises if any required signal is missing so a partial poll
    fails closed rather than silently passing.
    """
    missing = R6_REQUIRED_SIGNALS - set(observations)
    if missing:
        raise ValueError(f"preflight is missing required signals: {sorted(missing)}")
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
        # no active alarms / unresolved incidents.
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
