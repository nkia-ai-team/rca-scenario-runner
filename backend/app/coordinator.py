"""Persistent global lease/dirty state with fencing-token cleanup claims."""
from __future__ import annotations

import fcntl
import os
import re
import socket
import tempfile
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


_OWNER_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _new_process_owner() -> str:
    # pid alone repeats across container restarts (the runner is often pid 1),
    # so a random part makes each process its own owner.
    host = _OWNER_UNSAFE.sub("_", socket.gethostname())[:40] or "host"
    return f"{host}-{os.getpid()}-{uuid.uuid4().hex[:12]}"


# The runner process that writes a lease or a cleanup claim. Kept per process,
# not per coordinator object: every coordinator this process builds speaks for
# the same process.
PROCESS_OWNER = _new_process_owner()

# Owner markers this process holds open (and flocked) for its whole life. The
# kernel drops a flock when its process dies, however it dies, so "can I lock
# that owner's marker" answers "is that owner process gone" without pids,
# heartbeats or clocks.
_held_markers: dict[Path, int] = {}
_held_markers_lock = threading.Lock()


class ActiveLease(StrictModel):
    run_id: str
    scenario_id: str
    fencing_token: int = Field(gt=0)
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    # Runner process that holds the lease (PROCESS_OWNER). None in state
    # written before owners were recorded.
    owner: str | None = None


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
    # Runner process doing the cleanup; None in state written before owners
    # were recorded.
    owner: str | None = None


class CoordinatorState(StrictModel):
    next_fencing_token: int = Field(default=1, gt=0)
    active_lease: ActiveLease | None = None
    dirty_run: DirtyRun | None = None
    cleanup_claim: CleanupClaim | None = None


# DIRTY reasons a runner restart leaves (recover_dead_owners). No cleanup failed:
# the run was cut before or during one, and the DIRTY cleanup retry
# (app.dirty_retry) takes these as it takes a failed cleanup.
RESTARTED_MID_RUN = "runner_restarted_mid_run"
RESTARTED_DURING_CLEANUP = "runner_restarted_during_cleanup"
RESTART_DIRTY_REASONS = frozenset({RESTARTED_MID_RUN, RESTARTED_DURING_CLEANUP})


class RestartRecovery(StrictModel):
    """One lease or cleanup claim taken back from a runner process that is gone."""

    action: Literal["marked_dirty", "released_cleanup_claim"]
    run_id: str
    scenario_id: str | None
    fencing_token: int
    previous_owner: str | None
    # The interrupted cleanup's claimant, when a cleanup was what got cut.
    claimant: str | None = None
    reason: str


class GlobalCoordinator:
    """Small JSON store guarded by ``flock`` and atomic replacement."""

    def __init__(self, state_path: Path, *, owner: str | None = None) -> None:
        self.state_path = state_path
        self.lock_path = state_path.with_suffix(state_path.suffix + ".lock")
        self.owner = _OWNER_UNSAFE.sub("_", owner) if owner else PROCESS_OWNER
        self.owners_dir = state_path.with_name(state_path.name + ".owners")

    def _marker(self, owner: str) -> Path:
        return self.owners_dir / f"{_OWNER_UNSAFE.sub('_', owner)}.lock"

    def hold_owner_marker(self) -> None:
        """Lock this process's owner marker for the rest of its life.

        Called before anything is written under this owner, so a lease or claim
        naming an owner always has a marker that stays locked while that owner
        lives.
        """
        path = self._marker(self.owner)
        with _held_markers_lock:
            if path in _held_markers:
                return
            self.owners_dir.mkdir(parents=True, exist_ok=True)
            for _ in range(5):
                fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o640)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    os.close(fd)
                    raise RuntimeError(
                        f"owner marker is held by another process: {self.owner}"
                    ) from error
                # Between the open and the flock, another process's probe can
                # lock the still-unlocked file, take this owner for dead and
                # unlink it. The lock would then sit on a file nobody can find,
                # and every probe would read this live owner as gone (review
                # U10 L1). Hold the lock only if it is on the file at the path.
                try:
                    same = os.fstat(fd).st_ino == os.stat(path).st_ino
                except FileNotFoundError:
                    same = False
                if same:
                    _held_markers[path] = fd
                    return
                os.close(fd)
            raise RuntimeError(f"owner marker could not be held: {self.owner}")

    def owner_alive(self, owner: str) -> bool:
        """Whether the runner process ``owner`` still runs.

        Its marker stays locked while it lives. A missing marker or one that
        locks now means the process is gone; the stale marker is removed.
        """
        if _OWNER_UNSAFE.sub("_", owner) == self.owner:
            return True
        path = self._marker(owner)
        with _held_markers_lock:
            if path in _held_markers:
                return True
        try:
            fd = os.open(path, os.O_RDWR)
        except FileNotFoundError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            path.unlink(missing_ok=True)
            return False
        finally:
            os.close(fd)

    def other_live_owners(self) -> list[str]:
        """Other runner processes alive on this state (stale markers are swept)."""
        if not self.owners_dir.is_dir():
            return []
        live = []
        for marker in sorted(self.owners_dir.glob("*.lock")):
            if marker.stem != self.owner and self.owner_alive(marker.stem):
                live.append(marker.stem)
        return live

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
                owner=self.owner,
            )
            self.hold_owner_marker()
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
                owner=self.owner,
            )
            self.hold_owner_marker()
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
                owner=self.owner,
            )
            self.hold_owner_marker()
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

    def recover_dead_owners(self, *, now: datetime) -> list[RestartRecovery]:
        """Take back what a runner process that is gone left in the coordinator.

        A restart kills the process between a claim (or a lease) and its
        release. Nothing else ever releases them: a cleanup claim has no expiry
        and blocks every later claim and, through the lease or DIRTY run under
        it, every acquisition; the queue stays stopped until someone edits the
        state file (U9 note 4, review R1).

        Called once at startup, before the watchdog and the queue run:

        * A cleanup claim whose owner is gone is dropped. The cleanup it guarded
          did not finish, so whatever it was cleaning stays (or becomes) DIRTY.
        * A lease whose owner is gone was a run cut mid-way: its effect on the
          testbed is unknown, so it becomes DIRTY and waits for cleanup like any
          other DIRTY run.
        * Anything held by a live process, this one or another runner sharing
          the state, is left alone. So is everything under a live claim: that
          cleanup is still running somewhere.

        Owner-less entries come from a runner built before owners were
        recorded. They count as gone only when no other runner process is alive
        on this state; with another live runner around their owner cannot be
        told apart, and they are left as they are.
        """
        self.hold_owner_marker()
        with self._locked() as state:
            # Also sweeps the markers of every runner process that has gone, so
            # one empty marker per restart does not pile up.
            others_alive = bool(self.other_live_owners())

            def gone(owner: str | None) -> bool:
                if owner is not None:
                    return not self.owner_alive(owner)
                return not others_alive

            recovered: list[RestartRecovery] = []
            updated = state
            interrupted: CleanupClaim | None = None
            claim = state.cleanup_claim
            if claim is not None:
                if not gone(claim.owner):
                    return []
                interrupted = claim
                target = state.active_lease or state.dirty_run
                matches = (
                    target is not None
                    and target.run_id == claim.run_id
                    and target.fencing_token == claim.fencing_token
                )
                updated = updated.model_copy(update={"cleanup_claim": None})
                if state.dirty_run is not None or not matches:
                    # The DIRTY run stays DIRTY: its cleanup never finished.
                    recovered.append(
                        RestartRecovery(
                            action="released_cleanup_claim",
                            run_id=claim.run_id,
                            scenario_id=target.scenario_id if matches and target else None,
                            fencing_token=claim.fencing_token,
                            previous_owner=claim.owner,
                            claimant=claim.claimant,
                            reason=RESTARTED_DURING_CLEANUP,
                        )
                    )
            lease = updated.active_lease
            if lease is not None and updated.dirty_run is None and gone(lease.owner):
                during_cleanup = (
                    interrupted is not None
                    and interrupted.run_id == lease.run_id
                    and interrupted.fencing_token == lease.fencing_token
                )
                reason = (
                    RESTARTED_DURING_CLEANUP if during_cleanup else RESTARTED_MID_RUN
                )
                dirty = DirtyRun(
                    run_id=lease.run_id,
                    scenario_id=lease.scenario_id,
                    fencing_token=lease.fencing_token,
                    reason=reason,
                    marked_at=now,
                )
                updated = updated.model_copy(update={"active_lease": None, "dirty_run": dirty})
                recovered.append(
                    RestartRecovery(
                        action="marked_dirty",
                        run_id=lease.run_id,
                        scenario_id=lease.scenario_id,
                        fencing_token=lease.fencing_token,
                        previous_owner=lease.owner,
                        claimant=interrupted.claimant if during_cleanup and interrupted else None,
                        reason=reason,
                    )
                )
            elif interrupted is not None and not recovered:
                # The claim sat on a lease whose own owner still runs (another
                # runner's watchdog claimed a live runner's lease and died). The
                # lease is that live process's to close. A claim fences every
                # heartbeat while it stands, and a renewal that fails once stops
                # renewing for good (LeaseHeartbeat.renewing), so the run ends
                # on heartbeat_failed and is marked DIRTY by its owner, or its
                # lease expires and the owner's own watchdog, no longer
                # exempting it, claims and cleans it. Only the dead claim is
                # ours to drop. A heartbeat that never ran while the claim stood
                # (claim, death and this recovery inside one renewal interval)
                # is not fenced; that needs two runners on one state.
                recovered.append(
                    RestartRecovery(
                        action="released_cleanup_claim",
                        run_id=interrupted.run_id,
                        scenario_id=lease.scenario_id if lease is not None else None,
                        fencing_token=interrupted.fencing_token,
                        previous_owner=interrupted.owner,
                        claimant=interrupted.claimant,
                        reason=RESTARTED_DURING_CLEANUP,
                    )
                )
            if updated != state:
                self._write(updated)
            return recovered

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
