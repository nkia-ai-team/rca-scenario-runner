"""Take back leases and cleanup claims a previous runner process left behind.

A runner restart (container stop, crash, deploy) can land between a claim and
its release. The cleanup claim then never goes away and blocks every later
cleanup and acquisition until someone edits the state file (U9 note 4, review
R1). The coordinator decides what to take back (``recover_dead_owners``); this
module runs it once at startup and leaves the reason next to each run it
touched, because the log of the process that died is gone.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from app.coordinator import GlobalCoordinator, RestartRecovery
from app.production_runtime import atomic_json

logger = logging.getLogger(__name__)

RECORD = "restart-recovery.json"
SOURCE = "restart-recovery"


def _run_dir(runs_root: Path, run_id: str) -> Path | None:
    run_dir = runs_root / run_id
    if run_dir.parent != runs_root or run_dir.is_symlink() or not run_dir.is_dir():
        return None
    return run_dir


def _cleanup_already_succeeded(run_dir: Path) -> bool:
    """Success in either shape: the flat one the watchdog, a manual cleanup and
    the DIRTY retry write, or the controller's ``{"cleanup": {...}}``
    (``persist_session``), as ``app.dirty_retry.cleanup_failed`` reads them."""
    try:
        document = json.loads((run_dir / "cleanup.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(document, dict):
        return False
    if "succeeded" in document:
        return document["succeeded"] is True
    cleanup = document.get("cleanup")
    return isinstance(cleanup, dict) and cleanup.get("succeeded") is True


def recovered_by_restart(run_dir: Path, run_id: str, fencing_token: int) -> bool:
    """A restart recovery touched this run under this fencing token."""
    try:
        record = json.loads((run_dir / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(record, dict)
        and record.get("run_id") == run_id
        and record.get("fencing_token") == fencing_token
    )


def recover_after_restart(
    coordinator: GlobalCoordinator, runs_root: Path, *, now: datetime
) -> list[RestartRecovery]:
    """Run the coordinator recovery and record it on each affected run."""
    recovered = coordinator.recover_dead_owners(now=now)
    for item in recovered:
        logger.warning(
            "restart recovery: %s %s (token %s, previous owner %s, claimant %s): %s",
            item.action,
            item.run_id,
            item.fencing_token,
            item.previous_owner,
            item.claimant,
            item.reason,
        )
        run_dir = _run_dir(runs_root, item.run_id)
        if run_dir is None:
            continue
        atomic_json(
            run_dir / RECORD,
            {
                "schema_version": 1,
                "source": SOURCE,
                "at": now.isoformat(),
                **item.model_dump(),
            },
        )
        if item.claimant is not None and not _cleanup_already_succeeded(run_dir):
            # The interrupted cleanup did not succeed as far as anyone can
            # tell. Say so in the flat shape the watchdog and manual cleanup
            # write, ending no later than now. A cleanup.json that already
            # reports success is left alone: the run stays DIRTY regardless and
            # a later cleanup runs again, but that evidence is not overwritten.
            atomic_json(
                run_dir / "cleanup.json",
                {
                    "schema_version": 1,
                    "source": SOURCE,
                    "succeeded": False,
                    "reason": f"cleanup by {item.claimant} interrupted by runner restart",
                    "finished_at": now.isoformat(),
                },
            )
    return recovered
