"""Declarative controller schema and side-effect-free plan normalization."""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.adaptive import (
    AdaptiveLevel,
    AdaptiveMode,
    AdaptiveSpec,
    Condition,
    ConditionSet,
)
from app.observations import APPROVED_ADAPTERS


_DURATION = re.compile(r"^(\d+)(s|m|h)?$")
_CAPTURE_MODEL_PATH = "/var/lib/lucida/ai-models/stream-anomaly/global/v1/model.json"


def duration_seconds(value: object, *, field: str) -> int:
    """Parse an integer second count or a compact ``10s|2m|1h`` duration."""
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a duration")
    if isinstance(value, int):
        if value < 0:
            raise ValueError(f"{field} must be non-negative")
        return value
    if isinstance(value, str):
        match = _DURATION.fullmatch(value.strip())
        if match:
            amount = int(match.group(1))
            multiplier = {None: 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]
            return amount * multiplier
    raise ValueError(f"{field} must be seconds or a compact duration such as 2m")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AdapterQuery(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    # Derived from APPROVED_ADAPTERS rather than restated: this literal was a
    # second hand-maintained copy and it went stale — host_probe was added to the
    # runtime for the 2026-07-28 storage-IO trio (F02-H/F10-H/F10-P) but not here,
    # so loading their manifests raised a validation error and blocked the whole
    # deploy path (deploy-live-promotions.sh loads every manifest before publishing).
    adapter: str
    query_id: str = Field(min_length=1)
    freshness_sec: int = Field(gt=0)
    parameters: dict[str, bool | int | float | str] = Field(default_factory=dict)

    @field_validator("adapter")
    @classmethod
    def _adapter_is_approved(cls, value: str) -> str:
        if value not in APPROVED_ADAPTERS:
            raise ValueError(f"adapter is not approved: {value}")
        return value


class BaselinePlan(StrictModel):
    clean_window_sec: int = Field(ge=0)
    checks: list[str] = Field(min_length=1)


class CleanupPlan(StrictModel):
    required: bool = True
    order: Literal["reverse"] = "reverse"
    timeout_sec: int = Field(default=600, gt=0)


class RecoveryPlan(StrictModel):
    conditions: ConditionSet
    timeout_sec: int = Field(gt=0)


class CapturePlan(StrictModel):
    enabled: bool = True
    # Capture window contract v2 (spec-eval-data-capture §2.1, 2026-07-20):
    # scenario segment is [t1-10m, t2+20m]; the 2h normal lead-in moved to a
    # shared daily normal segment.
    pre_window_sec: int = Field(default=600, ge=0)
    post_window_sec: int = Field(default=1200, ge=0)
    model_snapshot: str = Field(min_length=1)
    create_golden_anomaly: Literal[False] = False

    @model_validator(mode="after")
    def validate_focused_window_policy(self) -> "CapturePlan":
        if self.pre_window_sec != 600 or self.post_window_sec != 1200:
            raise ValueError("capture window must be exactly [t1-10m,t2+20m]")
        if self.model_snapshot != _CAPTURE_MODEL_PATH:
            raise ValueError("capture model snapshot must use the canonical global v1 model path")
        return self


class ControllerSpec(StrictModel):
    """Normalized form stored on ``Scenario`` and emitted by dry-run."""

    tick_interval_sec: int = Field(gt=0)
    max_injection_duration_sec: int = Field(gt=0)
    adaptive: AdaptiveSpec
    preflight_checks: list[str] = Field(min_length=1)
    baseline: BaselinePlan
    adapter_queries: list[AdapterQuery] = Field(min_length=1)
    cleanup: CleanupPlan
    recovery: RecoveryPlan
    capture: CapturePlan

    @model_validator(mode="after")
    def validate_signal_bindings(self) -> "ControllerSpec":
        query_ids = [item.id for item in self.adapter_queries]
        if len(query_ids) != len(set(query_ids)):
            raise ValueError("controller observation ids must be unique")
        available = set(query_ids)
        sets = [
            self.adaptive.success,
            self.adaptive.escalate,
            self.adaptive.abort,
            self.adaptive.must_rule_out,
            self.recovery.conditions,
        ]
        referenced = {
            condition.signal
            for condition_set in sets
            if condition_set is not None
            for condition in condition_set.conditions
        }
        missing = sorted(referenced - available)
        if missing:
            raise ValueError(f"controller conditions reference undeclared observations: {missing}")
        return self


def _condition_set(raw: object, *, name: str, required: bool = True) -> ConditionSet | None:
    if raw is None:
        if required:
            raise ValueError(f"controller.{name} is required")
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"controller.{name} must be a mapping")
    consecutive = raw.get("consecutive_ticks", 1)
    if "conditions" in raw:
        return ConditionSet.model_validate(raw)
    matches = [key for key in ("all", "any") if key in raw]
    if len(matches) != 1:
        raise ValueError(f"controller.{name} must contain exactly one of all or any")
    match = matches[0]
    items = raw[match]
    if not isinstance(items, list):
        raise ValueError(f"controller.{name}.{match} must be a list")
    conditions = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"controller.{name}.{match}[{index}] must be a mapping")
        signal = item.get("observation", item.get("signal"))
        operator = item.get("op", item.get("operator"))
        conditions.append(
            Condition(
                id=str(item.get("id") or f"{name.replace('_', '-')}-{index}"),
                signal=signal,
                operator=operator,
                value=item.get("value"),
            )
        )
    return ConditionSet(match=match, conditions=conditions, consecutive_ticks=consecutive)


def _checks(raw: object) -> list[str]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("controller baseline.required must be a non-empty list")
    checks: list[str] = []
    for item in raw:
        check = item.get("check") if isinstance(item, dict) else item
        if not isinstance(check, str) or not check.strip():
            raise ValueError("controller baseline checks must have a non-empty check id")
        checks.append(check.strip())
    if len(checks) != len(set(checks)):
        raise ValueError("controller baseline check ids must be unique")
    return checks


def parse_controller(raw: object) -> ControllerSpec | None:
    """Compile the human-oriented YAML contract into deterministic core models."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("controller must be a mapping")

    mode = AdaptiveMode(raw.get("mode", "calibration"))
    profile = raw.get("profile")
    if not isinstance(profile, dict):
        raise ValueError("controller.profile must be a mapping")
    kind = profile.get("kind")
    expected_kind = "fixed" if mode == AdaptiveMode.EVALUATION else "adaptive_ladder"
    if kind != expected_kind:
        raise ValueError(f"controller {mode.value} mode requires profile.kind={expected_kind}")
    levels_raw = profile.get("levels")
    if not isinstance(levels_raw, list) or not levels_raw:
        raise ValueError("controller.profile.levels must be a non-empty list")
    if mode == AdaptiveMode.EVALUATION and len(levels_raw) != 1:
        raise ValueError("evaluation mode requires exactly one level")
    settle_sec = duration_seconds(raw.get("settle_after_change", 0), field="settle_after_change")
    max_duration = duration_seconds(
        raw.get("max_injection_duration", 1200), field="max_injection_duration"
    )
    levels = []
    for item in levels_raw:
        if not isinstance(item, dict):
            raise ValueError("controller profile levels must be mappings")
        levels.append(
            AdaptiveLevel(
                id=item.get("id"),
                parameters=item.get("parameters", {}),
                min_hold_sec=duration_seconds(item.get("min_hold", 0), field="min_hold"),
                settle_sec=duration_seconds(item.get("settle", settle_sec), field="settle"),
                timeout_sec=duration_seconds(item.get("timeout", max_duration), field="timeout"),
            )
        )

    observations_raw = raw.get("observations")
    if not isinstance(observations_raw, list):
        raise ValueError("controller.observations must be a list")
    observations = []
    for item in observations_raw:
        if not isinstance(item, dict):
            raise ValueError("controller observations must be mappings")
        if "query" in item and "query_id" not in item:
            raise ValueError("controller observations must use approved query_id, not raw query")
        observations.append(
            AdapterQuery(
                id=item.get("id"),
                adapter=item.get("adapter"),
                query_id=item.get("query_id"),
                freshness_sec=duration_seconds(item.get("freshness", 60), field="freshness"),
                parameters=item.get("parameters", {}),
            )
        )

    baseline_raw = raw.get("baseline")
    if not isinstance(baseline_raw, dict):
        raise ValueError("controller.baseline must be a mapping")
    recovery_raw = raw.get("recovery")
    if not isinstance(recovery_raw, dict):
        raise ValueError("controller.recovery must be a mapping")
    capture_raw = raw.get("capture", {})
    if not isinstance(capture_raw, dict):
        raise ValueError("controller.capture must be a mapping")
    cleanup_raw = raw.get("cleanup", {})
    if not isinstance(cleanup_raw, dict):
        raise ValueError("controller.cleanup must be a mapping")

    adaptive = AdaptiveSpec(
        mode=mode,
        levels=levels,
        success=_condition_set(raw.get("success"), name="success"),
        escalate=_condition_set(raw.get("escalate"), name="escalate", required=False),
        abort=_condition_set(raw.get("abort"), name="abort", required=False),
        must_rule_out=_condition_set(raw.get("must_rule_out"), name="must_rule_out"),
    )
    recovery_conditions = _condition_set(recovery_raw, name="recovery")
    assert recovery_conditions is not None
    return ControllerSpec(
        tick_interval_sec=duration_seconds(raw.get("tick_interval", 15), field="tick_interval"),
        max_injection_duration_sec=max_duration,
        adaptive=adaptive,
        preflight_checks=_checks(raw.get("preflight", baseline_raw.get("required"))),
        baseline=BaselinePlan(
            clean_window_sec=duration_seconds(
                baseline_raw.get("clean_window", "2h"), field="baseline.clean_window"
            ),
            checks=_checks(baseline_raw.get("required")),
        ),
        adapter_queries=observations,
        cleanup=CleanupPlan(
            required=cleanup_raw.get("required", True),
            order=cleanup_raw.get("order", "reverse"),
            timeout_sec=duration_seconds(cleanup_raw.get("timeout", "10m"), field="cleanup.timeout"),
        ),
        recovery=RecoveryPlan(
            conditions=recovery_conditions,
            timeout_sec=duration_seconds(recovery_raw.get("timeout", "10m"), field="recovery.timeout"),
        ),
        capture=CapturePlan(
            enabled=capture_raw.get("enabled", True),
            pre_window_sec=duration_seconds(capture_raw.get("pre_window", "10m"), field="capture.pre_window"),
            post_window_sec=duration_seconds(capture_raw.get("post_window", "20m"), field="capture.post_window"),
            model_snapshot=capture_raw.get(
                "model_snapshot",
                _CAPTURE_MODEL_PATH,
            ),
            create_golden_anomaly=capture_raw.get("create_golden_anomaly", False),
        ),
    )


def normalized_controller_plan(spec: ControllerSpec | None, *, scenario_id: str) -> dict[str, Any]:
    """Return a JSON-ready plan. The capture command is descriptive only."""
    if spec is None:
        return {
            "adaptive": None,
            "preflight_checks": [],
            "baseline": None,
            "adapter_queries": [],
            "cleanup": {"required": True, "order": "reverse"},
            "recovery": None,
            "capture": {"enabled": False, "execution": "planned_only", "command": None},
        }
    capture = spec.capture.model_dump(mode="json")
    capture.update(
        {
            "execution": "planned_only",
            "command": [
                "capture-eval-case.sh",
                "--case-id", "<case-id>",
                "--scenario-id", scenario_id.replace(":", "/scenario-"),
                "--t1", "<t1-utc>",
                "--t2", "<t2-utc>",
                "--case-label", "<calibration|evaluation|failed>",
            ],
        }
    )
    return {
        "adaptive": spec.adaptive.model_dump(mode="json"),
        "tick_interval_sec": spec.tick_interval_sec,
        "max_injection_duration_sec": spec.max_injection_duration_sec,
        "preflight_checks": spec.preflight_checks,
        "baseline": spec.baseline.model_dump(mode="json"),
        "adapter_queries": [item.model_dump(mode="json") for item in spec.adapter_queries],
        "cleanup": spec.cleanup.model_dump(mode="json"),
        "recovery": spec.recovery.model_dump(mode="json"),
        "capture": capture,
    }
