"""Background fenced cleanup for expired adaptive-controller leases."""
from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from collections.abc import Callable
from pathlib import Path

from app.adaptive_runtime import CleanupRequest
from app.coordinator import GlobalCoordinator
from app.production_runtime import RunArtifactStore, SystemClock, TrustedDispatcherApplier, atomic_json
from app.watchdog import WatchdogDecision, decide_watchdog


class WatchdogService:
    def __init__(
        self,
        coordinator: GlobalCoordinator,
        runs_root: Path,
        *,
        clock=None,
        heartbeat_timeout_sec: int = 30,
        applier_factory: Callable[..., object] = TrustedDispatcherApplier,
        live_owner: Callable[[str, int], bool] | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.runs_root = runs_root
        self.clock = clock or SystemClock()
        self.heartbeat_timeout_sec = heartbeat_timeout_sec
        self.applier_factory = applier_factory
        # Answers "is the run holding this lease still executing in this
        # process?". Such a lease is not orphaned, whatever its heartbeat says.
        self.live_owner = live_owner

    async def tick(self) -> WatchdogDecision:
        decision = decide_watchdog(
            self.coordinator.snapshot(),
            now=self.clock.now(),
            heartbeat_timeout_sec=self.heartbeat_timeout_sec,
        )
        if decision.action != "claim_cleanup":
            return decision
        assert decision.run_id is not None and decision.fencing_token is not None
        if self.live_owner is not None and self.live_owner(
            decision.run_id, decision.fencing_token
        ):
            # A stale heartbeat on a run that is still executing here means the
            # renewal was held up (a profile mutation holding the coordinator
            # lock past the lease, a stalled loop), not that the owner died.
            # Claiming it fenced the live run out mid-injection and left the
            # testbed DIRTY (F63-R-run-17139f4c, 2026-10-26).
            return WatchdogDecision(
                action="healthy",
                reason="lease_owner_running_in_process",
                run_id=decision.run_id,
                fencing_token=decision.fencing_token,
            )
        # Everything below waits on the coordinator file lock, hashes the
        # capsule and shells out to profile cleanup: never on the event loop.
        claimant = f"watchdog-{uuid.uuid4().hex[:12]}"
        try:
            claim = await asyncio.to_thread(
                self.coordinator.claim_expired_cleanup,
                now=self.clock.now(),
                heartbeat_timeout_sec=self.heartbeat_timeout_sec,
                claimant=claimant,
            )
            if claim.run_id != decision.run_id or claim.fencing_token != decision.fencing_token:
                raise RuntimeError("watchdog claim changed fenced target")
        except RuntimeError:
            return decide_watchdog(
                self.coordinator.snapshot(),
                now=self.clock.now(),
                heartbeat_timeout_sec=self.heartbeat_timeout_sec,
            )
        try:
            applier, request = await asyncio.to_thread(self._cleanup_call, decision)
            result = await asyncio.to_thread(applier.cleanup, request)
            if inspect.isawaitable(result):
                result = await result
            if result.succeeded:
                effect_ended_at = getattr(result, "effect_ended_at", None) or self.clock.now()
                atomic_json(
                    self.runs_root / decision.run_id / "cleanup.json",
                    {
                        "schema_version": 1,
                        "source": "watchdog",
                        "succeeded": True,
                        "effect_ended_at": effect_ended_at.isoformat(),
                    },
                )
                atomic_json(
                    self.runs_root / decision.run_id / "recovery.json",
                    {"schema_version": 1, "source": "watchdog", "status": "succeeded"},
                )
                await asyncio.to_thread(
                    self.coordinator.release,
                    run_id=decision.run_id,
                    fencing_token=decision.fencing_token,
                )
                return WatchdogDecision(action="idle", reason="watchdog_cleanup_recovered")
            reason = result.reason or "watchdog_cleanup_or_recovery_failed"
        except Exception as error:
            reason = f"watchdog_cleanup_failed:{type(error).__name__}"
        await asyncio.to_thread(
            self.coordinator.mark_dirty,
            run_id=decision.run_id,
            fencing_token=decision.fencing_token,
            reason=reason,
            now=self.clock.now(),
        )
        atomic_json(
            self.runs_root / decision.run_id / "cleanup.json",
            {"schema_version": 1, "source": "watchdog", "succeeded": False, "reason": reason},
        )
        atomic_json(
            self.runs_root / decision.run_id / "recovery.json",
            {"schema_version": 1, "source": "watchdog", "status": "failed", "reason": reason},
        )
        await asyncio.to_thread(
            self.coordinator.finish_cleanup_claim,
            run_id=decision.run_id,
            fencing_token=decision.fencing_token,
            claimant=claimant,
        )
        return WatchdogDecision(
            action="block_dirty",
            reason=reason,
            run_id=decision.run_id,
            fencing_token=decision.fencing_token,
        )

    def _cleanup_call(self, decision: WatchdogDecision):
        assert decision.run_id is not None and decision.fencing_token is not None
        run_dir = self.runs_root / decision.run_id
        capsule = RunArtifactStore(self.runs_root).verify_capsule(run_dir)
        plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
        binding = capsule["binding"]
        contracts = run_dir / "capsule" / "contracts"
        applier = self.applier_factory(
            binding["catalog_slug"],
            primary_profile=binding["primary_profile"],
            logical_profile_id=binding["logical_profile_id"],
            companion_profiles=binding["companion_profiles"],
            dispatcher=contracts / "run-scenario.sh",
            profile_control=contracts / "profile-control.py",
            run_dir=run_dir,
        )
        request = CleanupRequest(
            run_id=decision.run_id,
            scenario_id=plan["scenario"]["id"],
            fencing_token=decision.fencing_token,
            profile_id=binding["logical_profile_id"],
            idempotency_key=f"watchdog-cleanup:{decision.run_id}:{decision.fencing_token}",
            requested_at=self.clock.now(),
        )
        return applier, request
