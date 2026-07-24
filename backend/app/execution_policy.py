"""Static policy for matching fault semantics to realistic injection locations."""
from __future__ import annotations

from app.models import InjectionPoint


_ALLOWED_TRANSPORTS: dict[str, set[str]] = {
    "north_south": {"ssh"},
    "east_west": {"kubectl"},
    "database": {"kubectl", "ssh"},
    "node_resource": {"ssh", "local"},
    "container_resource": {"kubectl", "docker"},
    "external_mock": {"kubectl", "api"},
    "network_path": {"ssh"},
    "change": {"kubectl", "api"},
    "business_fault": {"kubectl", "api"},
    "composite_control": {"local"},
}


def validate_injection_policy(point: InjectionPoint) -> None:
    """Reject topology-distorting placement before any subprocess is created."""
    allowed = _ALLOWED_TRANSPORTS[point.kind]
    if point.transport not in allowed:
        raise ValueError(
            f"injection point {point.id}: {point.kind} requires transport in "
            f"{sorted(allowed)}, got {point.transport}"
        )
    location = point.location.lower()
    if point.kind == "north_south" and "tb-runner" not in location:
        raise ValueError(f"injection point {point.id}: north_south must run at tb-runner")
    if point.kind == "network_path" and not (
        "192.168.200.57" in location or "external" in location
    ):
        raise ValueError(
            f"injection point {point.id}: network_path must run at external .57"
        )


def validate_execution_plan(points: list[InjectionPoint]) -> None:
    for point in points:
        validate_injection_policy(point)
