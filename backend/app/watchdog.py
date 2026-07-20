"""Pure watchdog decision core; it never executes cleanup."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.coordinator import CoordinatorState


class WatchdogDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["idle", "healthy", "claim_cleanup", "cleanup_claimed", "block_dirty"]
    reason: str
    run_id: str | None = None
    fencing_token: int | None = None


class WatchdogRequest(BaseModel):
    now: datetime
    heartbeat_timeout_sec: int = Field(gt=0)


def decide_watchdog(
    state: CoordinatorState,
    *,
    now: datetime,
    heartbeat_timeout_sec: int,
) -> WatchdogDecision:
    """Describe the next action; callers separately compete for the cleanup claim."""
    if heartbeat_timeout_sec <= 0:
        raise ValueError("heartbeat_timeout_sec must be positive")
    target = state.active_lease or state.dirty_run
    if target is None:
        return WatchdogDecision(action="idle", reason="no_active_or_dirty_run")
    if state.cleanup_claim is not None:
        return WatchdogDecision(
            action="cleanup_claimed",
            reason="fenced_cleanup_already_claimed",
            run_id=target.run_id,
            fencing_token=target.fencing_token,
        )
    if state.dirty_run is not None:
        return WatchdogDecision(
            action="block_dirty",
            reason="dirty_run_requires_explicit_cleanup_and_recovery",
            run_id=target.run_id,
            fencing_token=target.fencing_token,
        )
    lease = state.active_lease
    assert lease is not None
    heartbeat_age = now.timestamp() - lease.heartbeat_at.timestamp()
    if now >= lease.expires_at or heartbeat_age >= heartbeat_timeout_sec:
        return WatchdogDecision(
            action="claim_cleanup",
            reason="lease_expired" if now >= lease.expires_at else "heartbeat_timeout",
            run_id=lease.run_id,
            fencing_token=lease.fencing_token,
        )
    return WatchdogDecision(
        action="healthy",
        reason="lease_and_heartbeat_current",
        run_id=lease.run_id,
        fencing_token=lease.fencing_token,
    )
