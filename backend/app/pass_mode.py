"""What kind of pass the live queue is running, and what that removes.

A dataset pass produces eval cases. Almost every expensive thing the queue does
between injections exists to produce or protect those cases: the export itself,
the 30m gap that keeps two capture windows `[t1-10m, t2+20m]` from overlapping,
the isolation gates that enforce that gap, the nightly protection of the shared
normal segment, the golden reset that gives each case a clean AI brain.

A smoke pass answers a different question — does each scenario work as designed
— and throws the case away. On 2026-07-31 those behaviours were discovered and
switched off one at a time, mid-batch, each after it had already stopped the
queue. They are one decision, so they belong behind one:

    SCENARIO_PASS=dataset   (default)
    SCENARIO_PASS=smoke

THE RULE THAT DECIDES WHAT MAY GO HERE
--------------------------------------
A smoke pass may skip only work whose sole purpose is producing or protecting
the eval case. It must never touch anything that decides pass/fail — injection
parameters and ladders, success/abort/must_rule_out conditions and thresholds,
min_hold, confirmation-window sample budgets, recovery verification, cleanup and
dirty semantics. The pass is validating two things: that the plumbing works, and
that each scenario means what it claims (F21-P, 2026-07-31, was mechanically
fine and still invalid — its success conditions and its discriminator were
mutually exclusive). Weakening the verdict would silently drop the second, which
is the half a static audit cannot do at all.

The preflight gate is NOT skipped, and gets stricter duty here: with the
isolation gates off it is the only thing separating one scenario's tail from the
next one's lead-in. It measures cleanliness instead of assuming it, which is why
it can carry that load — but note its eight signals do not watch Kafka consumer
backlog, JVM heap after a GC-pressure scenario, or a filled disk. Those tails
outlive a short gap. If spurious dirty verdicts show up in a smoke pass, that is
where to look first.
"""
from __future__ import annotations

import os
from datetime import timedelta

DATASET = "dataset"
SMOKE = "smoke"

# With the isolation gates off, the gap is whatever the preflight gate needs to
# call the window clean; this floor only keeps the queue from re-injecting into
# the fault it just cleaned up. The gate re-checks every PREFLIGHT_RECHECK_INTERVAL,
# so a shorter floor buys nothing.
SMOKE_INTER_SCENARIO_GAP = timedelta(minutes=5)
# Dataset grade: previous t2 to next t1 must exceed POST_WINDOW (20m) + PRE_WINDOW
# (10m) or the two capture windows overlap. Not a tunable preference — 15m was
# tried on 2026-07-31 and the runner's own isolation gates blocked every start,
# because each manifest's baseline.clean_window is 30m with a ge=1800 floor.
DATASET_INTER_SCENARIO_GAP_MIN = 30


def pass_mode() -> str:
    """The declared pass kind. Anything but an exact "smoke" means dataset.

    The default is the expensive, safe one: a dataset run silently stripped of
    its captures is an unrecoverable loss, since the fault window is gone once
    it passes, while a smoke pass that does too much only costs time.
    """
    return SMOKE if os.environ.get("SCENARIO_PASS", "").strip().lower() == SMOKE else DATASET


def is_smoke_pass() -> bool:
    return pass_mode() == SMOKE


def capture_enabled() -> bool:
    """Whether accepted runs are exported as eval cases."""
    return not is_smoke_pass()


def protection_window_enabled() -> bool:
    """Whether starts are deferred around the daily 00:00-02:00 KST clean window."""
    return not is_smoke_pass()


def isolation_checks_enabled() -> bool:
    """Whether the clean-window / scenario_overlap eligibility gates apply.

    These enforce capture-window separation. The v3 continuous cycle already
    opts out of them for the same reason a smoke pass does: there is no pair of
    capture windows to keep apart.
    """
    return not is_smoke_pass()


def golden_reset_enabled(configured: bool) -> bool:
    """Whether to reset AI state and restart the observer before each injection.

    `configured` is the operator's own GOLDEN_RESET_ENABLED setting; a smoke pass
    overrides it off, because a clean AI brain per case only matters to a case.
    """
    return configured and not is_smoke_pass()


def inter_scenario_gap() -> timedelta:
    """Minimum gap from the previous run's t2 to the next injection.

    In a dataset pass the value may be raised but not lowered past the point
    where two capture windows touch. Lowering it does not make such a pass
    faster — it only moves the rejection to the runner's isolation gates, which
    read each manifest's own baseline.clean_window (2026-07-31: 15m produced
    check_failed:clean-window + scenario_overlap on every start). A smoke pass
    is the supported way to go faster.
    """
    if is_smoke_pass():
        return SMOKE_INTER_SCENARIO_GAP
    configured = int(
        os.environ.get("SCENARIO_CLEAN_WINDOW_MIN", str(DATASET_INTER_SCENARIO_GAP_MIN))
    )
    return timedelta(minutes=max(configured, DATASET_INTER_SCENARIO_GAP_MIN))
