"""When the runner may call a failed cleanup again on its own.

A node injection (host.stress, host.job, host.firewall, network.fault) arms a
timer on the node before it changes anything, and the node undoes the change by
itself once the timer fires (testbed-services executor_common, U8). A cleanup
that could not reach the node (2026-10-17 F80-R: tb-w2 NotReady, ssh refused)
leaves the run DIRTY here even though the node will put itself back. Calling the
same cleanup after the node's deadline then finds nothing left to undo and
succeeds, which is what an operator's manual POST did.

This module only decides *when*. It never calls cleanup and never touches the
coordinator; the runner does both under the usual fenced claim.

The node's deadline, from the runner's side:

* Every arm happens inside an apply call (profile-control runs the executor's
  ``run``, the executor arms before it changes the node). The applier journals
  each call in ``mutations.json``: ``complete_at`` when profile-control returned,
  ``failed_at`` when it exited with an error. Both are after anything that call
  armed. This does not use the reported ``applied_at``: profile-control takes it
  *before* the executor runs, so an arm comes later by the executor's checks and
  lock waits (review U9 M1), and a failed apply reports nothing at all (H1). A
  call with only an intent (the process died in it) bounds nothing, and then the
  DIRTY mark does: profile-control holds the coordinator lock for the whole
  executor run and marking DIRTY needs that lock, so every arm precedes it.
* It fires ``max_injection_duration + NODE_UNDO_MARGIN`` after the arm.
* A cleanup that reaches the node postpones the timer to ``now + NODE_UNDO_HOLD``
  when less than that is left, and a cleanup that then fails leaves it there.
  Every attempt finished no later than the DIRTY mark or its own recorded end,
  so ``last cleanup end + NODE_UNDO_HOLD`` bounds that. An attempt cut off with
  the previous process has no end; the start of this process bounds it.

The retry waits for the later of the two plus ``slack`` (the undo itself takes up
to a few minutes: systemctl stop, a kill wait). Calling earlier would race the
node's own undo; the node serializes them with a lock, but the point is not to
lean on that. The same bound spaces later attempts, so the gap between attempts
is fixed at ``hold + slack`` after the previous attempt ended and never grows.

Only a DIRTY run whose recorded cleanup *failed* is retried. A run that cleaned
up and then failed recovery (``recovery_timeout``) or lost its heartbeat is DIRTY
for a reason a second cleanup cannot fix, and clearing it that way would hide it.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.controller import duration_seconds
from app.coordinator import CleanupClaim, DirtyRun
from app.production_runtime import atomic_json

logger = logging.getLogger(__name__)

RETRY_RECORD = "dirty-retry.json"
RETRY_SOURCE = "dirty-cleanup-retry"

# Mirrors testbed-services scripts/scenarios/profiles/executor_common.py:
# GUARD_MARGIN_SECONDS (600), GUARD_CLEANUP_HOLD_SECONDS (960) and
# DEFAULT_MAX_INJECTION_SECONDS (1200, the controller's own default here too).
# tests/test_external_live_manifests.py compares them with a checked-out
# testbed-services. They are also the floors: the environment may only wait
# longer, never call before the node's own deadline.
NODE_UNDO_MARGIN_SEC = 600
NODE_UNDO_HOLD_SEC = 960
DEFAULT_MAX_INJECTION_SEC = 1200
DEFAULT_SLACK_SEC = 300
MIN_SLACK_SEC = 60
DEFAULT_MAX_ATTEMPTS = 3

# One read-modify-write of a run's retry record at a time: the watchdog loop's
# waiting note and an attempt's finishing note run on different worker threads
# (review U9 L1: a late waiting note erased the last attempt and the give-up).
_RECORD_LOCK = threading.Lock()


def _env_seconds(name: str, default: int, floor: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = duration_seconds(raw, field=name)
    except ValueError:
        logger.warning("%s=%r is not a duration; using %ss", name, raw, default)
        return default
    if value < floor:
        logger.warning("%s=%ss is below its floor; using %ss", name, value, floor)
        return floor
    return value


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    slack_sec: int = DEFAULT_SLACK_SEC
    node_margin_sec: int = NODE_UNDO_MARGIN_SEC
    node_hold_sec: int = NODE_UNDO_HOLD_SEC

    def __post_init__(self) -> None:
        if (
            self.max_attempts < 0
            or self.slack_sec < MIN_SLACK_SEC
            or self.node_margin_sec < NODE_UNDO_MARGIN_SEC
            or self.node_hold_sec < NODE_UNDO_HOLD_SEC
        ):
            raise ValueError("dirty retry policy would call cleanup before the node's deadline")

    @classmethod
    def from_env(cls) -> "RetryPolicy":
        """Settings never stop the runner from starting: a bad value falls back."""
        raw_attempts = os.environ.get("DIRTY_RETRY_MAX_ATTEMPTS", "").strip()
        attempts = DEFAULT_MAX_ATTEMPTS
        if raw_attempts:
            try:
                attempts = int(raw_attempts)
            except ValueError:
                attempts = -1
            if attempts < 0:
                logger.warning(
                    "DIRTY_RETRY_MAX_ATTEMPTS=%r is not a count; using %s",
                    raw_attempts, DEFAULT_MAX_ATTEMPTS,
                )
                attempts = DEFAULT_MAX_ATTEMPTS
        return cls(
            max_attempts=attempts,
            slack_sec=_env_seconds("DIRTY_RETRY_SLACK_SEC", DEFAULT_SLACK_SEC, MIN_SLACK_SEC),
            node_margin_sec=_env_seconds(
                "DIRTY_RETRY_NODE_UNDO_MARGIN_SEC", NODE_UNDO_MARGIN_SEC, NODE_UNDO_MARGIN_SEC
            ),
            node_hold_sec=_env_seconds(
                "DIRTY_RETRY_NODE_UNDO_HOLD_SEC", NODE_UNDO_HOLD_SEC, NODE_UNDO_HOLD_SEC
            ),
        )


@dataclass(frozen=True)
class RetryDecision:
    due: bool
    reason: str
    attempt: int = 0  # the attempt number a due decision would make
    not_before: datetime | None = None
    node_undo_deadline: datetime | None = None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def read_record(run_dir: Path, dirty: DirtyRun) -> dict[str, Any]:
    """This DIRTY run's retry record, or a fresh one (another token is another run)."""
    record = _read(run_dir / RETRY_RECORD)
    if (
        record is None
        or record.get("run_id") != dirty.run_id
        or record.get("fencing_token") != dirty.fencing_token
        or not isinstance(record.get("attempts"), list)
    ):
        return {
            "schema_version": 1,
            "run_id": dirty.run_id,
            "fencing_token": dirty.fencing_token,
            "status": "waiting",
            "attempts": [],
        }
    return record


def cleanup_failed(run_dir: Path) -> bool:
    """The run's last recorded cleanup ran and failed.

    ``persist_session`` writes ``{"cleanup": {...}}``; the watchdog, a manual
    cleanup and this retry write the flat ``{"succeeded": ...}``.
    """
    document = _read(run_dir / "cleanup.json")
    if document is None:
        return False
    if "succeeded" in document:
        return document["succeeded"] is False
    cleanup = document.get("cleanup")
    return isinstance(cleanup, dict) and cleanup.get("succeeded") is False


def _max_injection_sec(run_dir: Path) -> int:
    plan = _read(run_dir / "plan.json") or {}
    runtime = (plan.get("controller") or {}).get("runtime") or {}
    try:
        value = duration_seconds(
            runtime.get("max_injection_duration", DEFAULT_MAX_INJECTION_SEC),
            field="max_injection_duration",
        )
    except ValueError:
        return DEFAULT_MAX_INJECTION_SEC
    return value if value > 0 else DEFAULT_MAX_INJECTION_SEC


def last_arm_bound(run_dir: Path, dirty: DirtyRun) -> datetime:
    """No arm on the node happened after this (see the module note)."""
    journal = _read(run_dir / "mutations.json") or {}
    operations = journal.get("operations")
    if not isinstance(operations, list):
        return dirty.marked_at
    ends: list[datetime] = []
    for operation in operations:
        if not isinstance(operation, dict) or operation.get("action") != "apply":
            continue
        moments = [
            moment
            for moment in (
                _parse_time(operation.get("complete_at")),
                _parse_time(operation.get("failed_at")),
            )
            if moment is not None
        ]
        if not moments:
            # Still running when the process went away: it may have armed at
            # any moment up to the DIRTY mark.
            return dirty.marked_at
        ends.append(max(moments))
    if not ends:
        return dirty.marked_at
    return min(dirty.marked_at, max(ends))


def last_cleanup_end(
    run_dir: Path,
    dirty: DirtyRun,
    record: dict[str, Any],
    *,
    process_started_at: datetime | None = None,
) -> datetime:
    """No cleanup that may have postponed the node's timer ended after this."""
    moments = [dirty.marked_at]
    cleanup = _read(run_dir / "cleanup.json") or {}
    moments.append(_parse_time(cleanup.get("finished_at")))
    error = _read(run_dir / "manual-cleanup-error.json") or {}
    moments.append(_parse_time(error.get("at")))
    for attempt in record.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        moments.append(_parse_time(attempt.get("finished_at")))
        if attempt.get("finished_at") is None:
            # Cut off with the process that ran it: its cleanup may have gone
            # on reaching the node until that process was gone (review U9 M2).
            moments.append(_parse_time(attempt.get("started_at")))
            moments.append(process_started_at)
    return max(moment for moment in moments if moment is not None)


def node_undo_deadline(
    run_dir: Path,
    dirty: DirtyRun,
    record: dict[str, Any],
    policy: RetryPolicy,
    *,
    process_started_at: datetime | None = None,
) -> datetime:
    armed = last_arm_bound(run_dir, dirty) + timedelta(
        seconds=_max_injection_sec(run_dir) + policy.node_margin_sec
    )
    postponed = last_cleanup_end(
        run_dir, dirty, record, process_started_at=process_started_at
    ) + timedelta(seconds=policy.node_hold_sec)
    return max(armed, postponed)


def decide(
    run_dir: Path,
    dirty: DirtyRun,
    *,
    now: datetime,
    policy: RetryPolicy,
    process_started_at: datetime | None = None,
) -> RetryDecision:
    if policy.max_attempts <= 0:
        return RetryDecision(due=False, reason="dirty_retry_disabled")
    if not (run_dir / "capsule.json").is_file():
        return RetryDecision(due=False, reason="no_recovery_capsule")
    record = read_record(run_dir, dirty)
    if record.get("status") == "gave_up":
        return RetryDecision(due=False, reason="dirty_retry_gave_up")
    if not cleanup_failed(run_dir):
        return RetryDecision(due=False, reason="dirty_not_from_a_failed_cleanup")
    attempts = len(record["attempts"])
    if attempts >= policy.max_attempts:
        return RetryDecision(due=False, reason="dirty_retry_attempts_exhausted")
    deadline = node_undo_deadline(
        run_dir, dirty, record, policy, process_started_at=process_started_at
    )
    not_before = deadline + timedelta(seconds=policy.slack_sec)
    if now < not_before:
        return RetryDecision(
            due=False,
            reason="waiting_for_node_undo_deadline",
            attempt=attempts + 1,
            not_before=not_before,
            node_undo_deadline=deadline,
        )
    return RetryDecision(
        due=True,
        reason="node_undo_deadline_passed",
        attempt=attempts + 1,
        not_before=not_before,
        node_undo_deadline=deadline,
    )


def note_waiting(run_dir: Path, dirty: DirtyRun, decision: RetryDecision) -> None:
    """Say when the next attempt comes, once per change (the loop ticks every few seconds)."""
    if decision.not_before is None:
        return
    with _RECORD_LOCK:
        record = read_record(run_dir, dirty)
        # The runner decides only while no attempt of its own runs, so a
        # "running" record here was cut off with an earlier process.
        if record.get("status") in {"gave_up", "succeeded"}:
            return
        not_before = decision.not_before.isoformat()
        if record.get("status") == "waiting" and record.get("not_before") == not_before:
            return
        atomic_json(
            run_dir / RETRY_RECORD,
            {
                **record,
                "status": "waiting",
                "reason": None,
                "not_before": not_before,
                "node_undo_deadline": decision.node_undo_deadline.isoformat()
                if decision.node_undo_deadline
                else None,
            },
        )


def note_blocked_by_claim(run_dir: Path, dirty: DirtyRun, claim: CleanupClaim) -> bool:
    """A cleanup claim nobody here holds blocks the retry; say so once.

    A claim is only released by its holder, so one left by a process that was
    killed mid-attempt blocks this retry and a manual cleanup alike. Reclaiming
    it is not this module's call; the record names it so the run does not just
    sit there. Returns whether this wrote a new note."""
    with _RECORD_LOCK:
        record = read_record(run_dir, dirty)
        reason = (
            f"cleanup claim held by {claim.claimant} since {claim.claimed_at.isoformat()} "
            "with no attempt running in this runner; retry waits until the claim is released"
        )
        if record.get("status") == "blocked" and record.get("reason") == reason:
            return False
        atomic_json(
            run_dir / RETRY_RECORD,
            {**record, "status": "blocked", "reason": reason, "not_before": None},
        )
        return True


def note_started(
    run_dir: Path, dirty: DirtyRun, *, claimant: str, started_at: datetime
) -> None:
    with _RECORD_LOCK:
        record = read_record(run_dir, dirty)
        atomic_json(
            run_dir / RETRY_RECORD,
            {
                **record,
                "status": "running",
                "reason": None,
                "not_before": None,
                "attempts": [
                    *record["attempts"],
                    {"claimant": claimant, "started_at": started_at.isoformat(), "finished_at": None},
                ],
            },
        )


def note_finished(
    run_dir: Path,
    dirty: DirtyRun,
    *,
    claimant: str,
    finished_at: datetime,
    succeeded: bool,
    reason: str | None,
    policy: RetryPolicy,
) -> dict[str, Any]:
    with _RECORD_LOCK:
        record = read_record(run_dir, dirty)
        attempts = list(record["attempts"])
        for index in range(len(attempts) - 1, -1, -1):
            if isinstance(attempts[index], dict) and attempts[index].get("claimant") == claimant:
                attempts[index] = {
                    **attempts[index],
                    "finished_at": finished_at.isoformat(),
                    "succeeded": succeeded,
                    "reason": reason,
                }
                break
        else:
            attempts.append(
                {
                    "claimant": claimant,
                    "started_at": None,
                    "finished_at": finished_at.isoformat(),
                    "succeeded": succeeded,
                    "reason": reason,
                }
            )
        if succeeded:
            status, summary = "succeeded", None
        elif len(attempts) >= policy.max_attempts:
            status = "gave_up"
            summary = (
                f"{len(attempts)} cleanup retries after the node undo deadline failed; "
                f"run stays DIRTY. last reason: {reason or 'unknown'}"
            )
        else:
            status, summary = "waiting", None
        updated = {
            **record,
            "attempts": attempts,
            "status": status,
            "reason": summary,
            "not_before": None,
        }
        atomic_json(run_dir / RETRY_RECORD, updated)
        return updated
