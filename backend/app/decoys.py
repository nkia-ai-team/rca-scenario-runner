"""Decoy and routine deployments inside a recording cycle (testbed v4, U7).

Why this exists: in the v3 recordings a deployment almost always *was* the
cause (59% of 82 cases were change-caused, only two carried an innocent
deployment), so "blame the latest rollout" answered most cases. The cure is not
fewer change scenarios but innocent changes that look the same: during the 2h
normal lead-in of every cycle the runner applies a few harmless deployments at
seeded random times, on random targets, of a random count (zero included).

Two user decisions (2026-11-09) narrow that: a decoy only goes to a target
*off* the fault path, and never to a target the answer key says did not change
(otherwise the decoy makes that answer-key sentence false). Both are computed
from the answer key when the schedule is drawn (``excluded_targets``), not kept
as a hand list. Until the product records Deployment changes (plan item P4),
the policy enables only the restart kind; the others stay in the code and are
switched on by weight.

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
import re
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
    # How far the fault path reaches: "mentioned" = every target the answer key
    # names anywhere in its facts (victims on the propagation path included);
    # "scored" = only root cause, trigger and scored names. Targets the answer
    # key says did not change are excluded under both.
    path_scope: Literal["mentioned", "scored"] = "mentioned"
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
        if self.relation_weights.get("on_path", 0) > 0:
            # User decision 2026-11-09: decoys stay off the fault path.
            raise ValueError("decoys must not target the fault path (on_path weight must be 0)")
        if self.rollout_timeout_sec >= self.windows["near_fault"].end_before_buffer_sec:
            # A rollout started at the last allowed moment must be over before
            # the buffer opens, or it bleeds into the preflight window.
            raise ValueError("rollout_timeout_sec must end before the buffer opens")
        missing = sorted(set(self.domain_namespaces) - set(_DOMAIN_WORDS))
        if missing:
            # U7b review M2: a domain without words would be read as "never
            # named" and its short names silently stop counting. Refuse the
            # policy (the queue pauses with this reason) until the words exist.
            raise ValueError(
                f"decoy policy domains {missing} have no domain words in app.decoys._DOMAIN_WORDS; "
                "add them before decoys may run for these domains"
            )
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


# A sentence says something did not change when it carries a change word and a
# negation (review2 B4: Korean restart words, lists such as "롤아웃, 새
# ReplicaSet, 파드 재시작이 없다", particles such as "배포나 재시작이 아님").
# Deliberately blunt: it also catches "재시작 없이 Ready" and symptom talk, and
# excluding a target too many is the safe side.
CHANGE_WORDS = re.compile(
    r"롤아웃|재배포|배포|변경|릴리스|재시작|재기동|ReplicaSet|rollout|redeploy|deploy|restart|release|change",
    re.IGNORECASE,
)
NEGATION = re.compile(
    r"없|아니|아님|\bno\b|\bnot\b|without|nothing|untouched|unchanged", re.IGNORECASE
)


def says_no_change(sentence: str) -> bool:
    return CHANGE_WORDS.search(sentence) is not None and NEGATION.search(sentence) is not None


# Fields describing *other* scenarios, not this one's facts.
_SKIP_FIELDS = frozenset({"related_scenarios"})
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。])\s+|\n+|[\u2460-\u2473]|;\s")
# Components that are not decoy targets. A no-change sentence that names only
# these says nothing about the app Deployments; one that names nothing at all
# ("nothing else changes") is read as covering the whole scenario namespace.
_NON_TARGET_WORDS = re.compile(
    r"mysql|oracle|postgres|redis|kafka|nginx|mock|노드|node|네트워크|network|스키마|schema|"
    r"인덱스|index|\bdb\b|데이터베이스|database|이미지|outbox|브로커|broker",
    re.IGNORECASE,
)


def _answer_key_texts(scenario: Mapping[str, Any], path: str = ""):
    if isinstance(scenario, str):
        yield path, scenario
    elif isinstance(scenario, Mapping):
        for key, value in scenario.items():
            if key in _SKIP_FIELDS:
                continue
            yield from _answer_key_texts(value, f"{path}.{key}" if path else str(key))
    elif isinstance(scenario, list):
        for index, value in enumerate(scenario):
            yield from _answer_key_texts(value, f"{path}[{index}]")


# Korean words the answer keys use for an app instead of its latin name. Only
# unambiguous ones: generic words such as 주문 or 결제 would match everything.
_KOREAN_NAMES: dict[str, tuple[str, ...]] = {"gateway": ("게이트웨이",)}
# Answer-key fields that are scored. Under path_scope="scored" every target they
# name is out, whatever the sentence says (review2 B4).
SCORED_FIELDS = frozenset({"root_cause", "scoring", "must_support", "must_rule_out"})


# Words that name a domain without naming one of its apps (the domain key from
# the policy always counts too). Used only to decide whether a short word such
# as ``api`` or ``order`` may point into another domain (U7b). Over-wide is the
# safe side: a word here only keeps more targets excluded.
_DOMAIN_WORDS: dict[str, tuple[str, ...]] = {
    "commerce": ("커머스", "checkout", "체크아웃"),
    "core-banking": ("banking", "은행"),
    "food-delivery": ("food", "배달", "음식"),
}
# Short names that also mean something else: everyday words an answer key uses
# for data, not apps ("in order", "application account", "ledger window").
# Short names two domains share (order, payment) are found from the policy.
# Only these are held to the domain boundary; any other short name (cart,
# pricing, gateway, restaurant, notify and every name a new domain brings)
# counts in every domain and also names its domain (U7b review M1).
_COMMON_SHORT_WORDS = frozenset({
    "order", "payment", "product", "inventory", "notification", "user",
    "api", "account", "transfer", "ledger", "dispatch",
})
# An HTTP route: a URL with a host, or a path that starts a token ("/api/...",
# "(/api/...)"). File paths inside source trees ("src/main/java/...") do not
# start a token and are not routes.
_ROUTE = re.compile(
    r"https?://[^\s/]+(?P<url>/[^\s)'\"`,]*)?|(?<![\w./-])(?P<path>/[A-Za-z{*][^\s)'\"`,]*)"
)


def _word_spans(text: str, word: str) -> list[tuple[int, int]]:
    return [
        match.span()
        for match in re.finditer(rf"(?<![A-Za-z0-9-]){re.escape(word)}(?![A-Za-z0-9-])", text)
    ]


def _word(text: str, word: str) -> bool:
    return bool(_word_spans(text, word))


def _route_prefixes(text: str) -> set[tuple[int, int]]:
    """Spans of the first segment of every HTTP route in ``text``.

    That segment (``api`` in ``/api/orders``, ``actuator`` in
    ``/actuator/health``) is the server's URL prefix, not the app the route
    belongs to, so it never names an app of another domain (D1 review N2: a
    commerce ``/api/orders`` read as banking ``testbed-api``). Later segments
    (``/api/inventory/reserve``) still count: they name the resource."""
    spans: set[tuple[int, int]] = set()
    for match in _ROUTE.finditer(text):
        group = "url" if match.group("url") else "path"
        if match.group(group) is None:
            continue
        start = match.start(group) + 1
        segment = re.match(r"[^/?#\s)'\"`,]*", text[start:])
        spans.add((start, start + len(segment.group(0)) if segment else start))
    return spans


def named_namespaces(policy: DecoyPolicy, scenario: Mapping[str, Any]) -> set[str]:
    """Namespaces the answer key names by something stronger than a short word:
    the namespace itself, a full app name (service name, alias, target id,
    Deployment, container) of any app in it, a unique short or Korean name of
    one of its apps (``cart``, 게이트웨이), or a word for its domain.

    A short word of another domain only counts as a mention when its domain is
    named here (U7b). A container shared by two domains (``order-service``)
    names both; too many is safe."""
    text = "\n".join(value for _, value in _answer_key_texts(scenario))
    lowered = text.lower()
    named: set[str] = set()
    for domain, namespace in policy.domain_namespaces.items():
        words = (domain, *_DOMAIN_WORDS[domain])
        if namespace in text or any(word.lower() in lowered for word in words):
            named.add(namespace)
    for target in policy.targets:
        if target.namespace in named:
            continue
        short = _short(target)
        if (
            any(name in text for name in target.names())
            or any(_word(text, word) for word in (target.deployment, target.container))
            or (not _ambiguous_short(policy, short) and _word(text, short))
            or any(korean in text for korean in _KOREAN_NAMES.get(short, ()))
        ):
            named.add(target.namespace)
    return named


def _short(target: DecoyTarget) -> str:
    return target.deployment.removeprefix("testbed-")


def _ambiguous_short(policy: DecoyPolicy, short: str) -> bool:
    """A short name that is an everyday word or belongs to two domains."""
    if short in _COMMON_SHORT_WORDS:
        return True
    return len({t.namespace for t in policy.targets if _short(t) == short}) > 1


def _scoped_names(policy: DecoyPolicy, text: str) -> tuple[set[str], str]:
    """Read product target names ``<namespace>/<object>`` as that one object.

    The product names a Kubernetes object it monitors by its namespace and
    name (``rca-testbed-food/testbed-external-pg-mock``), and the answer keys
    score by that name. Such a name points at one object, not at the whole
    namespace: it is a hit for the policy target whose Deployment it names in
    that namespace (``<namespace>/<deployment>`` or
    ``<namespace>/deployment/<deployment>``) and for nothing else (an external
    mock, a Job, a quota). The names are cut out of the text so that the
    namespace and Deployment readings below do not see them again (TRIG review
    2026-11-12: the namespace substring took every app of the domain off the
    decoy list)."""
    namespaces = sorted({target.namespace for target in policy.targets}, key=len, reverse=True)
    if not namespaces:
        return set(), text
    pattern = re.compile(
        r"(?<![A-Za-z0-9_.-])(" + "|".join(re.escape(ns) for ns in namespaces) + r")/([A-Za-z0-9_.:/-]+)"
    )
    hits: set[str] = set()
    for match in pattern.finditer(text):
        namespace, obj = match.group(1), match.group(2).split(":", 1)[0].rstrip("./")
        obj = obj.removeprefix("deployment/")
        for target in policy.targets:
            if target.namespace == namespace and obj == target.deployment:
                hits.add(target.key)
    return hits, pattern.sub(" ", text)


def mentioned_targets(
    policy: DecoyPolicy,
    text: str,
    *,
    home: str | None = None,
    named: set[str] | None = None,
) -> set[str]:
    """Policy target keys a piece of answer-key text names.

    Every way an answer key names an app counts, in every namespace (review2
    B3): service name, alias, target id, Deployment, container, the short word
    (``order``), its Korean name, or the whole namespace. A short word shared by
    two domains (``order``, ``payment``) excludes both; too many is safe.

    With ``home`` (the scenario's namespace) and ``named`` (from
    ``named_namespaces``) an ambiguous short word (an everyday word, or a name
    two domains share) is held to the domain boundary (U7b): for an app outside
    ``home`` it counts only when the answer key names that app's domain
    somewhere, and never as the first segment of an HTTP route. Inside ``home``
    it always counts. A unique short name and the Korean name count in every
    domain (review M1: a path listing "cart -> pricing" in another domain's
    answer key is still the fault path). Without
    ``home`` the reading stays blunt (every short word everywhere), which is
    what the no-change reading uses."""
    hits, text = _scoped_names(policy, text)
    prefixes = _route_prefixes(text) if home is not None else set()
    for target in policy.targets:
        if target.namespace in text or any(name in text for name in target.names()):
            hits.add(target.key)
            continue
        if any(_word(text, word) for word in (target.deployment, target.container)):
            hits.add(target.key)
            continue
        short = _short(target)
        if any(korean in text for korean in _KOREAN_NAMES.get(short, ())):
            hits.add(target.key)
            continue
        spans = _word_spans(text, short)
        if home is not None and target.namespace != home and _ambiguous_short(policy, short):
            if named is None or target.namespace not in named:
                continue
            spans = [span for span in spans if span not in prefixes]
        if spans:
            hits.add(target.key)
    return hits


def scenario_namespace(policy: DecoyPolicy, scenario: Mapping[str, Any]) -> str | None:
    return policy.domain_namespaces.get(scenario.get("domain"))  # type: ignore[arg-type]


def stated_unchanged(
    policy: DecoyPolicy, scenario: Mapping[str, Any]
) -> dict[str, list[tuple[str, str]]]:
    """Targets the answer key says did not change -> [(field, sentence)].

    A decoy rollout on such a target would make the sentence false for that
    recording, so these are never decoy targets (user decision 2026-11-09).
    The extraction is mechanical; it errs toward excluding."""
    namespace = scenario_namespace(policy, scenario)
    named = named_namespaces(policy, scenario)
    found: dict[str, list[tuple[str, str]]] = {}
    for field, text in _answer_key_texts(scenario):
        for sentence in _SENTENCE_SPLIT.split(text):
            sentence = sentence.strip()
            if not sentence or not says_no_change(sentence):
                continue
            # Blunt on purpose: every short word in every namespace stays out
            # (a no-change sentence wrongly read costs a false recording). The
            # "nothing else changed" fallback is decided on the bounded
            # reading, so a URL prefix or another domain's short word no longer
            # switches it off (U7 review3 M3).
            keys = mentioned_targets(policy, sentence)
            bounded = mentioned_targets(policy, sentence, home=namespace, named=named)
            if not bounded and namespace and not _NON_TARGET_WORDS.search(sentence):
                keys |= {t.key for t in policy.targets if t.namespace == namespace}
            for key in keys:
                found.setdefault(key, []).append((field, sentence))
    return found


def excluded_targets(
    policy: DecoyPolicy, scenario_id: str, scenario: Mapping[str, Any]
) -> dict[str, str]:
    """Target key -> why it may not carry a decoy in this scenario.

    In order: scored names (always the fault path), targets the answer key says
    did not change, any target the scored items (root cause, scoring,
    must_support, must_rule_out) name, then (``path_scope="mentioned"``) any
    other target the answer key names, then the policy's own per-scenario list.
    The path readings hold short words to the domain boundary
    (``mentioned_targets`` with ``home``); the no-change reading does not."""
    names = _scenario_names(scenario)
    reasons: dict[str, str] = {}
    for target in policy.targets:
        if target.names() & names:
            reasons[target.key] = "scored"
    for key in stated_unchanged(policy, scenario):
        reasons.setdefault(key, "stated_unchanged")
    home = scenario_namespace(policy, scenario)
    named = named_namespaces(policy, scenario)
    scored_text = "\n".join(
        value for field, value in _answer_key_texts(scenario)
        if field.split(".", 1)[0].split("[", 1)[0] in SCORED_FIELDS
    )
    for key in mentioned_targets(policy, scored_text, home=home, named=named):
        reasons.setdefault(key, "scored_item")
    if policy.path_scope == "mentioned":
        text = "\n".join(value for _, value in _answer_key_texts(scenario))
        for key in mentioned_targets(policy, text, home=home, named=named):
            reasons.setdefault(key, "mentioned")
    for key in policy.scenario_exclusions.get(scenario_id, []):
        reasons.setdefault(key, "policy")
    return reasons


def _usable_kinds(policy: DecoyPolicy, target: DecoyTarget) -> dict[str, int]:
    return {
        kind: weight
        for kind, weight in policy.kind_weights.items()
        if weight > 0 and (target.rollout_ok or kind not in ROLLOUT_KINDS)
    }


def decoy_candidates(
    policy: DecoyPolicy, scenario_id: str, scenario: Mapping[str, Any]
) -> dict[DecoyRelation, list[DecoyTarget]]:
    """Targets a decoy may use in this scenario, split by relation. Never on
    the fault path; ``same_domain`` is an unmentioned app in the scenario's own
    namespace, ``off_domain`` one in another namespace."""
    namespace = scenario_namespace(policy, scenario)
    groups: dict[DecoyRelation, list[DecoyTarget]] = {key: [] for key in RELATION_ORDER}
    if namespace is None:
        # review2 M3: without a domain the answer key cannot be read against
        # the targets reliably; no decoy rather than a wrong one.
        return groups
    excluded = excluded_targets(policy, scenario_id, scenario)
    for target in policy.targets:
        if target.key in excluded or not _usable_kinds(policy, target):
            continue
        relation: DecoyRelation = "same_domain" if target.namespace == namespace else "off_domain"
        groups[relation].append(target)
    return groups


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
    groups = decoy_candidates(policy, scenario_id, scenario)
    namespace = scenario_namespace(policy, scenario)
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
        kind = _weighted(rng, _usable_kinds(policy, target), KIND_ORDER)
        token = f"{rng.getrandbits(24):06x}"
        stamp = at.strftime("%Y%m%d%H%M")
        if kind == "configmap":
            records.append(
                DecoyRecord(
                    id=f"d{index}", window=window_name, planned_at=_format(at), kind=kind,
                    namespace=target.namespace,
                    relation="same_domain" if target.namespace == namespace else "off_domain",
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
# Label keys that name the alert's own subject. Matching any label value (for
# instance a peer or upstream label) would let a decoy excuse another service's
# alarm, so only these keys are read. vmalert label names on 119 were not
# observable when this was written; a label set that uses none of them is
# simply never excused, which keeps the gate closed (safe side).
SERVICE_LABEL_KEYS: tuple[str, ...] = ("service_name", "service", "otel_service_name")
TARGET_LABEL_KEYS: tuple[str, ...] = ("target_id", "targetId", "lucida_target_id")
WORKLOAD_LABEL_KEYS: tuple[str, ...] = ("deployment", "workload")
POD_LABEL_KEYS: tuple[str, ...] = ("pod", "pod_name")
NAMESPACE_LABEL_KEYS: tuple[str, ...] = ("namespace", "k8s_namespace")


def _label(labels: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = labels.get(key)
        if value is not None and str(value):
            return str(value)
    return None


def _alert_matches(alert: Mapping[str, Any], record: DecoyRecord) -> bool:
    labels = alert.get("labels") or {}
    if record.service_name and _label(labels, SERVICE_LABEL_KEYS) == record.service_name:
        return True
    if record.target_id and _label(labels, TARGET_LABEL_KEYS) == record.target_id:
        return True
    if record.deployment is None or _label(labels, NAMESPACE_LABEL_KEYS) != record.namespace:
        return False
    if _label(labels, WORKLOAD_LABEL_KEYS) == record.deployment:
        return True
    pod = _label(labels, POD_LABEL_KEYS)
    return pod is not None and pod.startswith(f"{record.deployment}-")


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
    now: datetime,
    buffer_start: datetime | None = None,
) -> tuple[list[Mapping[str, Any]], list[dict[str, Any]]]:
    """Split firing alerts into (still blocking, explained by a decoy).

    ``now`` is the preflight time and is required: a silent wall-clock default
    made a forgotten argument look like a fix (review2). An alert is explained only when all hold, otherwise it blocks:
    - the decoy rolled pods (restart kinds) and finished healthy (``applied``).
      A metadata-only decoy cannot cause an alarm, and a failed, undone or
      unfinished one explains nothing;
    - the alert names the decoy's target through its own subject labels
      (service name, target id, or namespace with Deployment or pod);
    - it began no earlier than the decoy and no later than the decoy's finish
      plus ``grace_sec``, and before the buffer opened (the buffer is the clean
      window the gate vouches for);
    - the preflight runs within ``grace_sec`` of the decoy's finish. A harmless
      restart settles in minutes; an alarm on its target still firing after
      that is residue, whatever started it.
    """
    now = now.astimezone(timezone.utc)
    applied = [
        record for record in decoys
        if record.kind in ROLLOUT_KINDS and record.status == "applied"
        and record.started_at and record.finished_at and record.deployment
    ]
    remaining: list[Mapping[str, Any]] = []
    excused: list[dict[str, Any]] = []
    for alert in alerts:
        started = _alert_started(alert)
        owner = None
        if started is not None:
            for record in applied:
                finished = _parse(record.finished_at)
                low = _parse(record.started_at)
                high = finished + timedelta(seconds=grace_sec)
                if buffer_start is not None:
                    high = min(high, buffer_start)
                if now > finished + timedelta(seconds=grace_sec):
                    continue
                if low <= started <= high and _alert_matches(alert, record):
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


# ---------------------------------------------------------------------------
# Report: how many decoy targets each scenario keeps (for people, not the queue)
# ---------------------------------------------------------------------------
def candidate_report(
    policy: DecoyPolicy, scenarios: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    rows = []
    for scenario_id in sorted(scenarios):
        scenario = scenarios[scenario_id]
        groups = decoy_candidates(policy, scenario_id, scenario)
        reasons = excluded_targets(policy, scenario_id, scenario)
        unchanged = stated_unchanged(policy, scenario)
        rows.append(
            {
                "scenario_id": scenario_id,
                "domain": scenario.get("domain"),
                "same_domain": [t.key for t in groups["same_domain"]],
                "off_domain": [t.key for t in groups["off_domain"]],
                "excluded": reasons,
                "unchanged_sentences": {
                    key: sorted(set(items)) for key, items in sorted(unchanged.items())
                },
            }
        )
    return rows


def _main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="decoy candidate report (markdown)")
    parser.add_argument("policy", type=Path)
    parser.add_argument("metadata", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    policy, digest = load_decoy_policy(args.policy)
    scenarios = json.loads(args.metadata.read_text(encoding="utf-8"))["scenarios"]
    rows = candidate_report(policy, scenarios)
    if args.json:
        print(json.dumps({"policy_sha256": digest, "rows": rows}, ensure_ascii=False, indent=1))
        return 0
    print(f"policy_sha256 {digest}\n")
    print(
        "| 시나리오 | 도메인 | 후보 계 | 같은 도메인 | 다른 도메인 "
        "| 제외(채점 이름, 채점 문항, 언급, 변경 없음 문장, 정책) |"
    )
    print("|---|---|---|---|---|---|")
    for row in rows:
        counts = {kind: sum(1 for why in row["excluded"].values() if why == kind)
                  for kind in ("scored", "scored_item", "mentioned", "stated_unchanged", "policy")}
        total = len(row["same_domain"]) + len(row["off_domain"])
        print(
            f"| {row['scenario_id']} | {row['domain']} | {total} | {len(row['same_domain'])} | "
            f"{len(row['off_domain'])} | {counts['scored']}, {counts['scored_item']}, "
            f"{counts['mentioned']}, {counts['stated_unchanged']}, {counts['policy']} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
