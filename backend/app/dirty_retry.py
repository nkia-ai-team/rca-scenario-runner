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

* The timer is armed by the apply call, so no later than the run's last
  successful apply (``applied_at`` of the last level change). An apply that
  failed or was cut by a runner error may have armed before failing; then the
  controller's ``terminal_at`` is the bound. A run whose session never reached a
  terminal record (the process died, the watchdog cleaned up) gives no such
  bound, and the DIRTY mark itself is used: every arm happened before it.
* It fires ``max_injection_duration + NODE_UNDO_MARGIN`` after the arm.
* A cleanup that reaches the node postpones the timer to ``now + NODE_UNDO_HOLD``
  when less than that is left, and a cleanup that then fails leaves it there.
  Every attempt finished no later than the DIRTY mark or the attempt's own
  recorded end, so ``last cleanup end + NODE_UNDO_HOLD`` bounds that.

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
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.controller import duration_seconds
from app.coordinator import DirtyRun
from app.production_runtime import atomic_json

RETRY_RECORD = "dirty-retry.json"
RETRY_SOURCE = "dirty-cleanup-retry"

# Mirrors testbed-services scripts/scenarios/profiles/executor_common.py:
# GUARD_MARGIN_SECONDS (600), GUARD_CLEANUP_HOLD_SECONDS (960) and
# DEFAULT_MAX_INJECTION_SECONDS (1200, the controller's own default here too).
# If the node side changes these, change them here (or set the environment).
NODE_UNDO_MARGIN_SEC = 600
NODE_UNDO_HOLD_SEC = 960
DEFAULT_MAX_INJECTION_SEC = 1200
DEFAULT_SLACK_SEC = 300
DEFAULT_MAX_ATTEMPTS = 3

# Controller reasons that end a run while an apply may have been in flight.
_APPLY_IN_FLIGHT_REASONS = ("profile_apply_failed", "runner_exception")


def _env_seconds(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return duration_seconds(raw.strip(), field=name)


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    slack_sec: int = DEFAULT_SLACK_SEC
    node_margin_sec: int = NODE_UNDO_MARGIN_SEC
    node_hold_sec: int = NODE_UNDO_HOLD_SEC

    @classmethod
    def from_env(cls) -> "RetryPolicy":
        raw_attempts = os.environ.get("DIRTY_RETRY_MAX_ATTEMPTS", "").strip()
        attempts = int(raw_attempts) if raw_attempts else DEFAULT_MAX_ATTEMPTS
        if attempts < 0:
            raise ValueError("DIRTY_RETRY_MAX_ATTEMPTS must be zero or more")
        return cls(
            max_attempts=attempts,
            slack_sec=_env_seconds("DIRTY_RETRY_SLACK_SEC", DEFAULT_SLACK_SEC),
            node_margin_sec=_env_seconds("NODE_UNDO_MARGIN_SEC", NODE_UNDO_MARGIN_SEC),
            node_hold_sec=_env_seconds("NODE_UNDO_HOLD_SEC", NODE_UNDO_HOLD_SEC),
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
    """No arm on the node happened after this."""
    state = _read(run_dir / "state.json")
    if state is None or state.get("status") != "dirty" or not state.get("cleanup"):
        # The session never closed here (watchdog cleanup after the process
        # went away): an apply may have been in flight and unrecorded.
        return dirty.marked_at
    bounds = [
        moment
        for change in state.get("level_changes") or []
        if isinstance(change, dict)
        and (moment := _parse_time(change.get("applied_at"))) is not None
    ]
    reason = str(((state.get("controller_state") or {}).get("reason")) or "")
    terminal = _parse_time(state.get("terminal_at"))
    if reason.startswith(_APPLY_IN_FLIGHT_REASONS):
        if terminal is None:
            return dirty.marked_at
        bounds.append(terminal)
    return max(bounds) if bounds else dirty.marked_at


def last_cleanup_end(run_dir: Path, dirty: DirtyRun, record: dict[str, Any]) -> datetime:
    """No cleanup that may have postponed the node's timer ended after this."""
    moments = [dirty.marked_at]
    cleanup = _read(run_dir / "cleanup.json") or {}
    moments.append(_parse_time(cleanup.get("finished_at")))
    error = _read(run_dir / "manual-cleanup-error.json") or {}
    moments.append(_parse_time(error.get("at")))
    for attempt in record.get("attempts") or []:
        if isinstance(attempt, dict):
            moments.append(_parse_time(attempt.get("finished_at")))
            # An attempt with a start and no end was cut (process stop): it may
            # have reached the node at any time up to now.
            if attempt.get("finished_at") is None:
                moments.append(_parse_time(attempt.get("started_at")))
    return max(moment for moment in moments if moment is not None)


def node_undo_deadline(
    run_dir: Path, dirty: DirtyRun, record: dict[str, Any], policy: RetryPolicy
) -> datetime:
    armed = last_arm_bound(run_dir, dirty) + timedelta(
        seconds=_max_injection_sec(run_dir) + policy.node_margin_sec
    )
    postponed = last_cleanup_end(run_dir, dirty, record) + timedelta(
        seconds=policy.node_hold_sec
    )
    return max(armed, postponed)


def decide(
    run_dir: Path, dirty: DirtyRun, *, now: datetime, policy: RetryPolicy
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
    deadline = node_undo_deadline(run_dir, dirty, record, policy)
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
    record = read_record(run_dir, dirty)
    not_before = decision.not_before.isoformat()
    if record.get("status") == "waiting" and record.get("not_before") == not_before:
        return
    atomic_json(
        run_dir / RETRY_RECORD,
        {
            **record,
            "status": "waiting",
            "not_before": not_before,
            "node_undo_deadline": decision.node_undo_deadline.isoformat()
            if decision.node_undo_deadline
            else None,
        },
    )


def note_started(
    run_dir: Path, dirty: DirtyRun, *, claimant: str, started_at: datetime
) -> None:
    record = read_record(run_dir, dirty)
    atomic_json(
        run_dir / RETRY_RECORD,
        {
            **record,
            "status": "running",
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
