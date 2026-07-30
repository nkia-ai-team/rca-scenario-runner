"""Approved observation queries and dependency-injected read-only adapters."""
from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar, Protocol

from app.adaptive import ObservedValue, Scalar


APPROVED_ADAPTERS = frozenset({
    "loadgen_summary",
    "http_probe",
    "prometheus",
    "kubernetes",
    "database",
    "host_probe",
    "business_probe",
    "capture_status",
})
FORBIDDEN_SCENARIO_KEYS = frozenset({"promql", "promql_template", "query", "query_text"})
DEFAULT_REGISTRY_PATH = Path(__file__).with_name("observation_queries.json")
# observed_at may legitimately sit slightly ahead of the reference clock: some
# adapters stamp with the remote store's evaluation time (e.g. VictoriaMetrics)
# or with a wall-clock read taken after the tick's reference `now`.  Without a
# bounded allowance those sub-second skews mark healthy signals stale and reset
# controller streaks (F03-G run ca64d788, 07-19).
CLOCK_SKEW_TOLERANCE_SEC = 10


class ObservationContractError(ValueError):
    pass


class ObservationBlocked(RuntimeError):
    def __init__(self, blocked: Mapping[str, ObservedValue]) -> None:
        self.blocked = dict(blocked)
        reasons = ", ".join(
            f"{query_id}:{value.freshness}/{value.quality}"
            for query_id, value in sorted(self.blocked.items())
        )
        super().__init__(f"observation polling blocked: {reasons}")


@dataclass(frozen=True)
class ApprovedQuery:
    query_id: str
    adapter: str
    freshness_sec: int
    # How often the *source* actually produces a new value. Distinct from
    # freshness_sec, which only bounds how stale a value may be before it is
    # unusable. 0 means the adapter queries the target at tick time, so every
    # tick is an independent sample; 60 means re-reading before a minute has
    # passed returns the same number again.
    update_interval_sec: int
    template_id: str | None
    selector: str | None
    request_ref: str | None
    parameters: Mapping[str, Scalar]


class ApprovedQueryRegistry:
    def __init__(self, document: Mapping[str, Any]) -> None:
        queries = document.get("queries")
        if not isinstance(queries, Mapping) or not queries:
            raise ObservationContractError("query registry requires a non-empty queries object")
        self._queries: dict[str, dict[str, Any]] = {}
        registered_adapters: set[str] = set()
        for query_id, raw in queries.items():
            if not isinstance(query_id, str) or not query_id:
                raise ObservationContractError("query ids must be non-empty strings")
            if not isinstance(raw, Mapping):
                raise ObservationContractError(f"query {query_id} must be an object")
            forbidden = FORBIDDEN_SCENARIO_KEYS.intersection(raw)
            if forbidden:
                raise ObservationContractError(
                    f"query {query_id} contains forbidden raw query fields: {sorted(forbidden)}"
                )
            adapter = raw.get("adapter")
            if adapter not in APPROVED_ADAPTERS:
                raise ObservationContractError(f"query {query_id} uses unknown adapter: {adapter}")
            registered_adapters.add(adapter)
            freshness_sec = raw.get("freshness_sec")
            if isinstance(freshness_sec, bool) or not isinstance(freshness_sec, int) or freshness_sec <= 0:
                raise ObservationContractError(f"query {query_id} requires positive freshness_sec")
            update_interval_sec = raw.get("update_interval_sec")
            if (
                isinstance(update_interval_sec, bool)
                or not isinstance(update_interval_sec, int)
                or update_interval_sec < 0
            ):
                raise ObservationContractError(
                    f"query {query_id} requires a non-negative update_interval_sec"
                )
            allowed_parameters = raw.get("allowed_parameters", [])
            if (
                not isinstance(allowed_parameters, list)
                or not all(isinstance(item, str) and item for item in allowed_parameters)
                or len(allowed_parameters) != len(set(allowed_parameters))
            ):
                raise ObservationContractError(f"query {query_id} has invalid parameter allowlist")
            if adapter == "prometheus" and not raw.get("template_id"):
                raise ObservationContractError(
                    f"prometheus query {query_id} requires an approved template_id"
                )
            self._queries[query_id] = dict(raw)
        if registered_adapters != APPROVED_ADAPTERS:
            raise ObservationContractError(
                "query registry must cover exactly the seven approved adapters"
            )

    @classmethod
    def from_path(cls, path: Path = DEFAULT_REGISTRY_PATH) -> "ApprovedQueryRegistry":
        with path.open(encoding="utf-8") as handle:
            document = json.load(handle)
        if not isinstance(document, Mapping):
            raise ObservationContractError("query registry document must be an object")
        return cls(document)

    def bind(self, scenario_request: Mapping[str, Any]) -> ApprovedQuery:
        if not isinstance(scenario_request, Mapping):
            raise ObservationContractError("observation request must be an object")
        forbidden = FORBIDDEN_SCENARIO_KEYS.intersection(scenario_request)
        if forbidden:
            raise ObservationContractError(
                f"raw scenario query text is forbidden: {sorted(forbidden)}"
            )
        extra = set(scenario_request) - {"query_id", "parameters"}
        if extra:
            raise ObservationContractError(f"unsupported observation request fields: {sorted(extra)}")
        query_id = scenario_request.get("query_id")
        if query_id not in self._queries:
            raise ObservationContractError(f"unknown approved query_id: {query_id}")
        parameters = scenario_request.get("parameters", {})
        if not isinstance(parameters, Mapping):
            raise ObservationContractError(f"query {query_id} parameters must be an object")
        spec = self._queries[query_id]
        allowed = set(spec.get("allowed_parameters", []))
        unknown = set(parameters) - allowed
        if unknown:
            raise ObservationContractError(
                f"query {query_id} parameters are not allowlisted: {sorted(unknown)}"
            )
        if not all(value is not None and _is_scalar(value) for value in parameters.values()):
            raise ObservationContractError(f"query {query_id} parameters must be scalar")
        return ApprovedQuery(
            query_id=query_id,
            adapter=spec["adapter"],
            freshness_sec=spec["freshness_sec"],
            update_interval_sec=spec["update_interval_sec"],
            template_id=spec.get("template_id"),
            selector=spec.get("selector"),
            request_ref=spec.get("request_ref"),
            parameters=dict(parameters),
        )


class ObservationReader(Protocol):
    def __call__(self, query: ApprovedQuery) -> Mapping[str, Any] | Awaitable[Mapping[str, Any]]:
        ...


Clock = Callable[[], datetime]


class ReadOnlyObservationAdapter:
    adapter_id: ClassVar[str]

    def __init__(self, reader: ObservationReader, *, clock: Clock | None = None) -> None:
        self._reader = reader
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def observe(self, query: ApprovedQuery) -> ObservedValue:
        now = _aware_utc(self._clock())
        if query.adapter != self.adapter_id:
            raise ObservationContractError(
                f"adapter {self.adapter_id} cannot execute query for {query.adapter}"
            )
        try:
            raw = self._reader(query)
            if inspect.isawaitable(raw):
                raw = await raw
            return _normalize(raw, query=query, now=now)
        except ObservationContractError:
            raise
        except Exception:
            return ObservedValue(
                value=None,
                observed_at=now,
                source=f"{self.adapter_id}:{query.query_id}",
                freshness="fresh",
                quality="error",
            )


class LoadgenSummaryAdapter(ReadOnlyObservationAdapter):
    adapter_id = "loadgen_summary"


class HttpProbeAdapter(ReadOnlyObservationAdapter):
    adapter_id = "http_probe"


class PrometheusAdapter(ReadOnlyObservationAdapter):
    adapter_id = "prometheus"


class KubernetesAdapter(ReadOnlyObservationAdapter):
    adapter_id = "kubernetes"


class DatabaseAdapter(ReadOnlyObservationAdapter):
    adapter_id = "database"


class HostProbeAdapter(ReadOnlyObservationAdapter):
    adapter_id = "host_probe"


class BusinessProbeAdapter(ReadOnlyObservationAdapter):
    adapter_id = "business_probe"


class CaptureStatusAdapter(ReadOnlyObservationAdapter):
    adapter_id = "capture_status"


class ObservationPoller:
    def __init__(
        self,
        registry: ApprovedQueryRegistry,
        adapters: Mapping[str, ReadOnlyObservationAdapter],
    ) -> None:
        missing = APPROVED_ADAPTERS - set(adapters)
        extra = set(adapters) - APPROVED_ADAPTERS
        if missing or extra:
            raise ObservationContractError(
                f"adapter set mismatch: missing={sorted(missing)}, extra={sorted(extra)}"
            )
        self._registry = registry
        self._adapters = dict(adapters)

    async def poll(
        self,
        requests: Sequence[Mapping[str, Any]],
    ) -> dict[str, ObservedValue]:
        observations: dict[str, ObservedValue] = {}
        for request in requests:
            query = self._registry.bind(request)
            if query.query_id in observations:
                raise ObservationContractError(f"duplicate query_id in poll: {query.query_id}")
            observations[query.query_id] = await self._adapters[query.adapter].observe(query)
        blocked = {
            query_id: observed
            for query_id, observed in observations.items()
            if not observed.usable or observed.observed_at is None
        }
        if blocked:
            raise ObservationBlocked(blocked)
        return observations


def _normalize(raw: Mapping[str, Any], *, query: ApprovedQuery, now: datetime) -> ObservedValue:
    if not isinstance(raw, Mapping):
        return _error_value(query, now)
    try:
        observed_at_raw = raw["observed_at"]
        if isinstance(observed_at_raw, str):
            observed_at = datetime.fromisoformat(observed_at_raw.replace("Z", "+00:00"))
        elif isinstance(observed_at_raw, datetime):
            observed_at = observed_at_raw
        else:
            return _error_value(query, now)
        observed_at = _aware_utc(observed_at)
        source = raw["source"]
        value = raw.get("value")
        if not isinstance(source, str) or not source or not _is_scalar(value):
            return _error_value(query, now)
        quality = raw.get("quality", "good")
        if quality not in {"good", "error"}:
            return _error_value(query, now)
        age_sec = (now - observed_at).total_seconds()
        freshness = (
            "fresh"
            if -CLOCK_SKEW_TOLERANCE_SEC <= age_sec <= query.freshness_sec
            else "stale"
        )
        error_detail = raw.get("error")
        return ObservedValue(
            value=value if quality == "good" else None,
            observed_at=observed_at,
            source=source,
            freshness=freshness,
            quality=quality,
            error=error_detail if quality == "error" and isinstance(error_detail, str) else None,
            update_interval_sec=query.update_interval_sec,
        )
    except (KeyError, TypeError, ValueError):
        return _error_value(query, now)


def _error_value(query: ApprovedQuery, now: datetime) -> ObservedValue:
    return ObservedValue(
        value=None,
        observed_at=now,
        source=f"{query.adapter}:{query.query_id}",
        freshness="fresh",
        quality="error",
        update_interval_sec=query.update_interval_sec,
    )


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ObservationContractError("observation timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (bool, int, float, str))
