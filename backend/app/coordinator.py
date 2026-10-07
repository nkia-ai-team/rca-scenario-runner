"""Persistent global lease/dirty state with fencing-token cleanup claims."""
from __future__ import annotations

import fcntl
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ActiveLease(StrictModel):
    run_id: str
    scenario_id: str
    fencing_token: int = Field(gt=0)
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime


class DirtyRun(StrictModel):
    run_id: str
    scenario_id: str
    fencing_token: int = Field(gt=0)
    reason: str
    marked_at: datetime


class CleanupClaim(StrictModel):
    run_id: str
    fencing_token: int = Field(gt=0)
    claimant: str
    claimed_at: datetime


class CoordinatorState(StrictModel):
    next_fencing_token: int = Field(default=1, gt=0)
    active_lease: ActiveLease | None = None
    dirty_run: DirtyRun | None = None
    cleanup_claim: CleanupClaim | None = None


class GlobalCoordinator:
    """Small JSON store guarded by ``flock`` and atomic replacement."""

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self.lock_path = state_path.with_suffix(state_path.suffix + ".lock")

    def snapshot(self) -> CoordinatorState:
        if not self.state_path.exists():
            return CoordinatorState()
        return CoordinatorState.model_validate_json(self.state_path.read_text(encoding="utf-8"))

    @contextmanager
    def _locked(self) -> Iterator[CoordinatorState]:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            state = self.snapshot()
            yield state

    def _write(self, state: CoordinatorState) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(prefix=f".{self.state_path.name}.", dir=self.state_path.parent)
        temp_path = Path(raw_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(state.model_dump_json(indent=2))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.state_path)
        finally:
            temp_path.unlink(missing_ok=True)

    def acquire(
        self,
        *,
        run_id: str,
        scenario_id: str,
        now: datetime,
        lease_sec: int,
    ) -> ActiveLease:
        if lease_sec <= 0:
            raise ValueError("lease_sec must be positive")
        with self._locked() as state:
            if state.dirty_run is not None:
                raise RuntimeError(f"dirty run blocks acquisition: {state.dirty_run.run_id}")
            if state.active_lease is not None:
                raise RuntimeError(f"active lease blocks acquisition: {state.active_lease.run_id}")
            token = state.next_fencing_token
            lease = ActiveLease(
                run_id=run_id,
                scenario_id=scenario_id,
                fencing_token=token,
                acquired_at=now,
                heartbeat_at=now,
                expires_at=datetime.fromtimestamp(now.timestamp() + lease_sec, timezone.utc),
            )
            updated = state.model_copy(
                update={
                    "next_fencing_token": token + 1,
                    "active_lease": lease,
                    "cleanup_claim": None,
                }
            )
            self._write(updated)
            return lease

    def heartbeat(self, *, run_id: str, fencing_token: int, now: datetime, lease_sec: int) -> ActiveLease:
        if lease_sec <= 0:
            raise ValueError("lease_sec must be positive")
        with self._locked() as state:
            lease = self._require_lease(state, run_id, fencing_token)
            if state.cleanup_claim is not None:
                raise RuntimeError("cleanup claim is a terminal fence for heartbeat")
            updated_lease = lease.model_copy(
                update={
                    "heartbeat_at": now,
                    "expires_at": datetime.fromtimestamp(now.timestamp() + lease_sec, timezone.utc),
                }
            )
            self._write(state.model_copy(update={"active_lease": updated_lease}))
            return updated_lease

    def release(self, *, run_id: str, fencing_token: int) -> None:
        with self._locked() as state:
            self._require_lease(state, run_id, fencing_token)
            self._write(state.model_copy(update={"active_lease": None, "cleanup_claim": None}))

    def mark_dirty(self, *, run_id: str, fencing_token: int, reason: str, now: datetime) -> DirtyRun:
        with self._locked() as state:
            lease = self._require_lease(state, run_id, fencing_token)
            dirty = DirtyRun(
                run_id=run_id,
                scenario_id=lease.scenario_id,
                fencing_token=fencing_token,
                reason=reason,
                marked_at=now,
            )
            self._write(state.model_copy(update={"active_lease": None, "dirty_run": dirty}))
            return dirty

    def readopt_dirty(
        self,
        *,
        run_id: str,
        scenario_id: str,
        fencing_token: int,
        reason: str,
        now: datetime,
    ) -> DirtyRun:
        """Take back a dirty run the coordinator no longer remembers.

        `mark_dirty` needs the run to still hold the lease, which is right while
        it is running. But a run can end dirty and then have the coordinator
        cleared out from under it by whatever took the lease next — the run's own
        record still says dirty and its effect interval is still open, so it goes
        on blocking every clean window with no supported way to close it.
        Re-adopting it restores the precondition external cleanup needs.

        Only from a genuinely idle coordinator: if a lease, another dirty run, or
        a cleanup claim is live, that is the state of record and this must not
        overwrite it.
        """
        with self._locked() as state:
            if state.active_lease is not None:
                raise RuntimeError("cannot readopt while a run holds the lease")
            if state.dirty_run is not None:
                raise RuntimeError("cannot readopt while another dirty run is held")
            if state.cleanup_claim is not None:
                raise RuntimeError("cannot readopt while a cleanup is claimed")
            dirty = DirtyRun(
                run_id=run_id,
                scenario_id=scenario_id,
                fencing_token=fencing_token,
                reason=reason,
                marked_at=now,
            )
            self._write(state.model_copy(update={"dirty_run": dirty}))
            return dirty

    def claim_cleanup(
        self, *, run_id: str, fencing_token: int, claimant: str, now: datetime
    ) -> CleanupClaim:
        with self._locked() as state:
            target = state.active_lease or state.dirty_run
            if target is None or target.run_id != run_id or target.fencing_token != fencing_token:
                raise RuntimeError("cleanup claim does not match the current fenced run")
            if state.cleanup_claim is not None:
                raise RuntimeError(f"cleanup already claimed by {state.cleanup_claim.claimant}")
            claim = CleanupClaim(
                run_id=run_id,
                fencing_token=fencing_token,
                claimant=claimant,
                claimed_at=now,
            )
            self._write(state.model_copy(update={"cleanup_claim": claim}))
            return claim

    def claim_expired_cleanup(
        self,
        *,
        now: datetime,
        heartbeat_timeout_sec: int,
        claimant: str,
    ) -> CleanupClaim:
        """Atomically recheck lease expiry/heartbeat before watchdog cleanup."""
        if heartbeat_timeout_sec <= 0:
            raise ValueError("heartbeat_timeout_sec must be positive")
        with self._locked() as state:
            lease = state.active_lease
            if lease is None or state.dirty_run is not None:
                raise RuntimeError("watchdog cleanup requires an active lease")
            if state.cleanup_claim is not None:
                raise RuntimeError(f"cleanup already claimed by {state.cleanup_claim.claimant}")
            heartbeat_age = now.timestamp() - lease.heartbeat_at.timestamp()
            if now < lease.expires_at and heartbeat_age < heartbeat_timeout_sec:
                raise RuntimeError("active lease was renewed before cleanup claim")
            claim = CleanupClaim(
                run_id=lease.run_id,
                fencing_token=lease.fencing_token,
                claimant=claimant,
                claimed_at=now,
            )
            self._write(state.model_copy(update={"cleanup_claim": claim}))
            return claim

    def clear_dirty(
        self, *, run_id: str, fencing_token: int, recovery_verified: bool
    ) -> None:
        if not recovery_verified:
            raise RuntimeError("dirty state requires successful recovery validation")
        with self._locked() as state:
            dirty = state.dirty_run
            claim = state.cleanup_claim
            if dirty is None or dirty.run_id != run_id or dirty.fencing_token != fencing_token:
                raise RuntimeError("dirty clear does not match the current fenced run")
            if claim is None or claim.run_id != run_id or claim.fencing_token != fencing_token:
                raise RuntimeError("dirty clear requires the fenced cleanup claim")
            self._write(state.model_copy(update={"dirty_run": None, "cleanup_claim": None}))

    def finish_cleanup_claim(
        self, *, run_id: str, fencing_token: int, claimant: str
    ) -> None:
        """Release a completed attempt while preserving DIRTY for a later retry."""
        with self._locked() as state:
            claim = state.cleanup_claim
            if (
                claim is None
                or claim.run_id != run_id
                or claim.fencing_token != fencing_token
                or claim.claimant != claimant
            ):
                raise RuntimeError("cleanup claim release rejected by fencing token or claimant")
            self._write(state.model_copy(update={"cleanup_claim": None}))

    @staticmethod
    def _require_lease(state: CoordinatorState, run_id: str, token: int) -> ActiveLease:
        lease = state.active_lease
        if lease is None or lease.run_id != run_id or lease.fencing_token != token:
            raise RuntimeError("operation rejected by fencing token")
        return lease


def get_coordinator() -> GlobalCoordinator:
    return GlobalCoordinator(
        Path(os.environ.get("COORDINATOR_STATE_PATH", "/app/state/coordinator.json"))
    )
