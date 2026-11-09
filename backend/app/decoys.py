"""Decoy and routine deployments inside a recording cycle (testbed v4, U7).

Why this exists: in the v3 recordings a deployment almost always *was* the
cause (59% of 82 cases were change-caused, only two carried an innocent
deployment), so "blame the latest rollout" answered most cases. The cure is not
fewer change scenarios but innocent changes that look the same: during the 2h
normal lead-in of every cycle the runner applies a few harmless deployments at
seeded random times, on random targets (on the fault path and off it), of
random kinds, and of a random count (zero included).

Shape of the work, split so every rule is testable without a cluster:

- ``DecoyPolicy``: the testbed-services registry file
  ``registry/decoy-deployments.json`` (targets, windows, weights). The runner
  only reads it.
- ``build_schedule``: pure. Same policy + seed + scenario + cycle start gives the
  same schedule, which is what makes a recording reproducible and auditable.
- ``KubectlDecoyExecutor``: the only part that touches the cluster. Every call
  is short (a patch, or one ``get``); the queue polls rollout progress on later
  ticks instead of blocking on ``rollout status``.
- ``partition_decoy_alarms``: pure. Lets the preflight gate tell an alarm a
  decoy rollout explains from residue it must still refuse.

What a decoy never does: change an image (the image drift guard compares each
Deployment with its own manifest image), touch a StatefulSet, Redis, a mock or
nginx, restart a ``maxSurge: 0`` Deployment (that would be an outage), or run
inside the buffer, the injection or the cooldown.
"""
from __future__ import annotations

import hashlib
import json
import random
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DecoyKind = Literal["restart", "config_rollout", "configmap", "annotation"]
DecoyWindowName = Literal["normal", "near_fault"]
DecoyRelation = Literal["on_path", "same_domain", "off_domain"]
DecoyStatus = Literal["planned", "rolling", "applied", "failed", "rolled_back", "skipped"]

ROLLOUT_KINDS: frozenset[str] = frozenset({"restart", "config_rollout"})
WINDOW_ORDER: tuple[DecoyWindowName, ...] = ("normal", "near_fault")
RELATION_ORDER: tuple[DecoyRelation, ...] = ("on_path", "same_domain", "off_domain")
KIND_ORDER: tuple[DecoyKind, ...] = ("restart", "config_rollout", "configmap", "annotation")
# Drawing a time that keeps min_gap from the others is retried this many times
# before the draw is dropped (a crowded window then simply yields fewer decoys).
_TIME_DRAW_ATTEMPTS = 64
KUBECTL_TIMEOUT_SEC = 30


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DecoyWindow(_Strict):
    count_weights: dict[str, int]
    start_after_cycle_start_sec: int | None = Field(default=None, ge=0)
    start_before_buffer_sec: int | None = Field(default=None, ge=0)
    end_before_buffer_sec: int = Field(ge=0)

    @model_validator(mode="after")
    def _one_start_anchor(self) -> "DecoyWindow":
        anchors = [self.start_after_cycle_start_sec, self.start_before_buffer_sec]
        if sum(anchor is not None for anchor in anchors) != 1:
            raise ValueError("a decoy window needs exactly one start anchor")
        if not self.count_weights or any(
            not key.isdigit() or weight < 0 for key, weight in self.count_weights.items()
        ):
            raise ValueError("count_weights must map non-negative counts to weights")
        if sum(self.count_weights.values()) <= 0:
            raise ValueError("count_weights must not be all zero")
        return self

    def bounds(self, cycle_start: datetime, buffer_start: datetime) -> tuple[datetime, datetime]:
        if self.start_after_cycle_start_sec is not None:
            low = cycle_start + timedelta(seconds=self.start_after_cycle_start_sec)
        else:
            low = buffer_start - timedelta(seconds=self.start_before_buffer_sec or 0)
        return low, buffer_start - timedelta(seconds=self.end_before_buffer_sec)


class DecoyTarget(_Strict):
    namespace: str = Field(min_length=1)
    deployment: str = Field(min_length=1)
    container: str = Field(min_length=1)
    service_name: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    rollout_ok: bool
    aliases: list[str] = Field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.deployment}"

    def names(self) -> set[str]:
        return {self.service_name, self.target_id, *self.aliases}


class DecoyConfigMap(_Strict):
    name: str = Field(min_length=1)
    key: str = Field(min_length=1)


class DecoyPolicy(_Strict):
    schema_version: Literal[1]
    note: str = Field(default="", alias="_note")
    windows: dict[DecoyWindowName, DecoyWindow]
    min_normal_sec: int = Field(ge=0)
    min_gap_sec: int = Field(ge=0)
    late_limit_sec: int = Field(ge=0)
    rollout_timeout_sec: int = Field(gt=0)
    alarm_excuse_grace_sec: int = Field(ge=0)
    kind_weights: dict[DecoyKind, int]
    relation_weights: dict[DecoyRelation, int]
    configmap: DecoyConfigMap
    annotation_key: str = Field(min_length=1)
    scenario_exclusions: dict[str, list[str]] = Field(default_factory=dict)
    domain_namespaces: dict[str, str]
    targets: list[DecoyTarget] = Field(min_length=1)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _coherent(self) -> "DecoyPolicy":
        keys = [target.key for target in self.targets]
        if len(keys) != len(set(keys)):
            raise ValueError("decoy targets must be unique per namespace/deployment")
        if set(self.windows) != set(WINDOW_ORDER):
            raise ValueError("decoy policy needs both the normal and the near_fault window")
        if sum(self.kind_weights.values()) <= 0 or sum(self.relation_weights.values()) <= 0:
            raise ValueError("kind and relation weights must not be all zero")
        if self.rollout_timeout_sec >= self.windows["near_fault"].end_before_buffer_sec:
            # A rollout started at the last allowed moment must be over before
            # the buffer opens, or it bleeds into the preflight window.
            raise ValueError("rollout_timeout_sec must end before the buffer opens")
        return self


class DecoyRecord(_Strict):
    """One planned decoy and, once it ran, what actually happened."""

    id: str
    window: DecoyWindowName
    planned_at: str
    kind: DecoyKind
    namespace: str
    # None for a ConfigMap-only change (no Deployment is touched).
    deployment: str | None = None
    service_name: str | None = None
    target_id: str | None = None
    relation: DecoyRelation
    configmap: str | None = None
    value: str
    status: DecoyStatus = "planned"
    started_at: str | None = None
    finished_at: str | None = None
    detail: str | None = None


def load_decoy_policy(path: Path) -> tuple[DecoyPolicy, str]:
    raw = path.read_bytes()
    return DecoyPolicy.model_validate_json(raw), hashlib.sha256(raw).hexdigest()


def cycle_seed(base: str, *parts: str | None) -> int:
    """Seed for one cycle, reproducible from values the recording keeps."""
    material = "\x1f".join([base, *[part or "" for part in parts]])
    return int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest()[:8], "big") >> 1


def _format(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _weighted(rng: random.Random, weights: Mapping[str, int], order: Sequence[str]) -> str:
    items = [(key, weights.get(key, 0)) for key in order if weights.get(key, 0) > 0]
    total = sum(weight for _, weight in items)
    pick = rng.uniform(0, total)
    running = 0.0
    for key, weight in items:
        running += weight
        if pick <= running:
            return key
    return items[-1][0]


def _scenario_names(scenario: Mapping[str, Any]) -> set[str]:
    """Names the answer key puts on the fault path (root cause, trigger, scored)."""
    root = scenario.get("root_cause") or {}
    scoring = scenario.get("scoring") or {}
    raw: list[Any] = [root.get("target_id"), root.get("trigger_target_id")]
    raw += list(scoring.get("accept") or []) + list(scoring.get("partial") or [])
    names: set[str] = set()
    for item in raw:
        if isinstance(item, str) and item:
            names.add(item)
            names.add(item.split(":", 1)[0])
    return names


def classify_targets(
    policy: DecoyPolicy, scenario: Mapping[str, Any]
) -> tuple[dict[DecoyRelation, list[DecoyTarget]], set[str]]:
    """Split the policy targets into on-path, same-domain and off-domain."""
    names = _scenario_names(scenario)
    domain = scenario.get("domain")
    scenario_namespaces = {policy.domain_namespaces[domain]} if domain in policy.domain_namespaces else set()
    groups: dict[DecoyRelation, list[DecoyTarget]] = {key: [] for key in RELATION_ORDER}
    for target in policy.targets:
        if target.names() & names:
            groups["on_path"].append(target)
            scenario_namespaces.add(target.namespace)
    for target in policy.targets:
        if target in groups["on_path"]:
            continue
        relation: DecoyRelation = "same_domain" if target.namespace in scenario_namespaces else "off_domain"
        groups[relation].append(target)
    return groups, scenario_namespaces


def _draw_times(
    rng: random.Random, count: int, low: datetime, high: datetime,
    taken: list[datetime], min_gap: timedelta,
) -> list[datetime]:
    span = int((high - low).total_seconds())
    if span < 0:
        return []
    drawn: list[datetime] = []
    for _ in range(count):
        for _attempt in range(_TIME_DRAW_ATTEMPTS):
            candidate = low + timedelta(seconds=rng.randint(0, span))
            if all(abs(candidate - other) >= min_gap for other in [*taken, *drawn]):
                drawn.append(candidate)
                break
    return drawn


def build_schedule(
    policy: DecoyPolicy,
    *,
    seed: int,
    scenario_id: str,
    scenario: Mapping[str, Any],
    cycle_start: datetime,
    buffer_start: datetime,
) -> list[DecoyRecord]:
    """Plan the decoys of one cycle. Pure and deterministic in ``seed``."""
    if (buffer_start - cycle_start).total_seconds() < policy.min_normal_sec:
        return []
    rng = random.Random(seed)
    groups, scenario_namespaces = classify_targets(policy, scenario)
    excluded = set(policy.scenario_exclusions.get(scenario_id, []))
    groups = {
        relation: [target for target in targets if target.key not in excluded]
        for relation, targets in groups.items()
    }
    min_gap = timedelta(seconds=policy.min_gap_sec)
    timed: list[tuple[datetime, DecoyWindowName]] = []
    for window_name in WINDOW_ORDER:
        window = policy.windows[window_name]
        count = int(_weighted(rng, window.count_weights, sorted(window.count_weights, key=int)))
        low, high = window.bounds(cycle_start, buffer_start)
        times = _draw_times(rng, count, low, high, [at for at, _ in timed], min_gap)
        timed.extend((at, window_name) for at in times)
    timed.sort(key=lambda item: item[0])

    records: list[DecoyRecord] = []
    for index, (at, window_name) in enumerate(timed, start=1):
        relation_weights = {
            relation: weight
            for relation, weight in policy.relation_weights.items()
            if groups.get(relation)
        }
        if not relation_weights:
            break
        relation = _weighted(rng, relation_weights, RELATION_ORDER)
        target = rng.choice(sorted(groups[relation], key=lambda item: item.key))
        kind_weights = {
            kind: weight
            for kind, weight in policy.kind_weights.items()
            if target.rollout_ok or kind not in ROLLOUT_KINDS
        }
        kind = _weighted(rng, kind_weights, KIND_ORDER)
        token = f"{rng.getrandbits(24):06x}"
        stamp = at.strftime("%Y%m%d%H%M")
        if kind == "configmap":
            records.append(
                DecoyRecord(
                    id=f"d{index}", window=window_name, planned_at=_format(at), kind=kind,
                    namespace=target.namespace,
                    relation="same_domain" if target.namespace in scenario_namespaces else "off_domain",
                    configmap=policy.configmap.name, value=f"{stamp}-{token}",
                )
            )
            continue
        records.append(
            DecoyRecord(
                id=f"d{index}", window=window_name, planned_at=_format(at), kind=kind,
                namespace=target.namespace, deployment=target.deployment,
                service_name=target.service_name, target_id=target.target_id,
                relation=relation,
                configmap=policy.configmap.name if kind == "config_rollout" else None,
                value=f"revision {token}" if kind == "annotation" else f"{stamp}-{token}",
            )
        )
    return records


# ---------------------------------------------------------------------------
# Cluster side
# ---------------------------------------------------------------------------
ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]


class KubectlDecoyExecutor:
    """Apply one decoy and read its rollout state with short kubectl calls."""

    def __init__(
        self,
        policy: DecoyPolicy,
        *,
        kubeconfig: str = "/root/tb-kubeconfig",
        process_runner: ProcessRunner = subprocess.run,
    ) -> None:
        self.policy = policy
        self.kubeconfig = kubeconfig
        self.process_runner = process_runner

    def _kubectl(self, namespace: str, *args: str, stdin: str | None = None) -> str:
        result = self.process_runner(
            ["kubectl", "--kubeconfig", self.kubeconfig, "-n", namespace, *args],
            input=stdin, capture_output=True, text=True, timeout=KUBECTL_TIMEOUT_SEC, check=False,
        )
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip().splitlines()
            raise RuntimeError(f"kubectl {args[0]} failed: {tail[-1] if tail else result.returncode}")
        return result.stdout

    def _patch_configmap(self, record: DecoyRecord) -> None:
        patch = json.dumps({"data": {self.policy.configmap.key: record.value}})
        self._kubectl(
            record.namespace, "patch", "configmap", record.configmap or self.policy.configmap.name,
            "--type", "merge", "-p", patch,
        )

    def apply(self, record: DecoyRecord) -> None:
        if record.kind in {"configmap", "config_rollout"}:
            self._patch_configmap(record)
        if record.kind == "annotation":
            self._kubectl(
                record.namespace, "annotate", f"deployment/{record.deployment}",
                f"{self.policy.annotation_key}={record.value}", "--overwrite",
            )
        if record.kind in ROLLOUT_KINDS:
            self._kubectl(record.namespace, "rollout", "restart", f"deployment/{record.deployment}")

    def rollout_done(self, record: DecoyRecord) -> bool:
        """Same completion rule as the profile executors' ``healthy``: status
        fields, not ``rollout status`` (a stale ProgressDeadlineExceeded would
        otherwise outlive a successful rollout)."""
        document = json.loads(
            self._kubectl(record.namespace, "get", "deployment", str(record.deployment), "-o", "json")
        )
        spec = int((document.get("spec") or {}).get("replicas", 1))
        status = document.get("status") or {}
        metadata = document.get("metadata") or {}
        return (
            int(status.get("observedGeneration", 0)) >= int(metadata.get("generation", 0))
            and int(status.get("updatedReplicas", 0)) == spec
            and int(status.get("availableReplicas", 0)) == spec
            and int(status.get("replicas", 0)) == spec
        )

    def undo(self, record: DecoyRecord) -> None:
        self._kubectl(record.namespace, "rollout", "undo", f"deployment/{record.deployment}")


# ---------------------------------------------------------------------------
# Preflight: which firing alarms a decoy explains
# ---------------------------------------------------------------------------
def _alert_values(alert: Mapping[str, Any]) -> set[str]:
    labels = alert.get("labels") or {}
    return {str(value) for value in labels.values() if value is not None}


def _alert_matches(values: set[str], record: DecoyRecord) -> bool:
    if record.service_name and record.service_name in values:
        return True
    if record.target_id and record.target_id in values:
        return True
    if record.deployment is None or record.namespace not in values:
        return False
    return any(
        value == record.deployment or value.startswith(f"{record.deployment}-") for value in values
    )


def _alert_started(alert: Mapping[str, Any]) -> datetime | None:
    raw = alert.get("activeAt") or alert.get("active_at")
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.strip().replace("Z", "+00:00")
    if "." in text:
        head, _, tail = text.partition(".")
        zone = tail[tail.find("+"):] if "+" in tail else (tail[tail.find("-"):] if "-" in tail else "")
        text = head + zone
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def partition_decoy_alarms(
    alerts: Iterable[Mapping[str, Any]],
    decoys: Iterable[DecoyRecord],
    *,
    grace_sec: int,
) -> tuple[list[Mapping[str, Any]], list[dict[str, Any]]]:
    """Split firing alerts into (still blocking, explained by a decoy).

    An alert is explained only when all hold, otherwise it keeps blocking:
    - it names the decoy's target (service name, target id, or the Deployment
      or one of its pods together with the namespace);
    - the decoy actually finished healthy (``applied``): a decoy that failed,
      was rolled back or never ran explains nothing;
    - the alert began inside [decoy start - 60s, decoy finish + grace]. An alert
      that was already firing before the decoy, or began long after it, is not
      the decoy's.
    """
    applied = [
        record for record in decoys
        if record.status == "applied" and record.started_at and record.finished_at
        and (record.deployment or record.service_name)
    ]
    remaining: list[Mapping[str, Any]] = []
    excused: list[dict[str, Any]] = []
    for alert in alerts:
        values = _alert_values(alert)
        started = _alert_started(alert)
        owner = None
        if started is not None:
            for record in applied:
                low = _parse(record.started_at) - timedelta(seconds=60)
                high = _parse(record.finished_at) + timedelta(seconds=grace_sec)
                if low <= started <= high and _alert_matches(values, record):
                    owner = record
                    break
        if owner is None:
            remaining.append(alert)
            continue
        excused.append(
            {
                "decoy_id": owner.id,
                "labels": dict(alert.get("labels") or {}),
                "active_at": alert.get("activeAt") or alert.get("active_at"),
            }
        )
    return remaining, excused


def decoy_document(
    *,
    scenario_id: str,
    seed: int | None,
    policy_sha256: str | None,
    cycle_start: str | None,
    records: Sequence[DecoyRecord],
) -> dict[str, Any]:
    """The ``decoys.json`` the capture carries into meta.json verbatim."""
    return {
        "schema_version": 1,
        "scenario_id": scenario_id,
        "seed": seed,
        "policy_sha256": policy_sha256,
        "cycle_start": cycle_start,
        "decoys": [record.model_dump(mode="json") for record in records],
    }
