from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.adaptive import (
    AdaptiveLevel,
    AdaptiveMode,
    AdaptiveSpec,
    Condition,
    ConditionSet,
    ControllerPhase,
)
from app.adaptive_runtime import (
    AdaptiveRuntime,
    ApplyRequest,
    ApplyResult,
    CleanupRequest,
    CleanupResult,
    ControllerSession,
    EligibilityEvidence,
    SessionStatus,
)
from app.controller import (
    AdapterQuery,
    BaselinePlan,
    CapturePlan,
    CleanupPlan,
    ControllerSpec,
    RecoveryPlan,
)
from app.observations import (
    ApprovedQueryRegistry,
    BusinessProbeAdapter,
    CaptureStatusAdapter,
    ClickHouseAdapter,
    DatabaseAdapter,
    HttpProbeAdapter,
    HostProbeAdapter,
    KubernetesAdapter,
    LoadgenSummaryAdapter,
    ObservationPoller,
    PrometheusAdapter,
)


START = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self) -> None:
        self.value = START

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class FakeEligibility:
    def __init__(self, clock: FakeClock, *, baseline_active: bool = True) -> None:
        self.clock = clock
        self.baseline_active = baseline_active
        self.requests = []

    def inspect(self, request):
        self.requests.append(request)
        return EligibilityEvidence(
            checked_at=self.clock.now(),
            source="fake:baseline-store",
            quality="good",
            check_results={check: True for check in request.checks},
            clean_window_start=self.clock.now() - timedelta(hours=2),
            clean_window_end=self.clock.now(),
            overlapping_run_ids=[],
            baseline_active=self.baseline_active,
        )


class DelayedEligibility(FakeEligibility):
    def inspect(self, request):
        self.clock.advance(1)
        return super().inspect(request)


class FakeApplier:
    def __init__(self, clock: FakeClock, *, cleanup_succeeds: bool = True) -> None:
        self.clock = clock
        self.cleanup_succeeds = cleanup_succeeds
        self.applies: list[ApplyRequest] = []
        self.cleanups: list[CleanupRequest] = []
        self.transition_cleanups: list[CleanupRequest] = []
        self.transition_gap_sec = 0

    def apply(self, request: ApplyRequest) -> ApplyResult:
        self.applies.append(request)
        return ApplyResult(
            run_id=request.run_id,
            fencing_token=request.fencing_token,
            idempotency_key=request.idempotency_key,
            applied_at=self.clock.now(),
        )

    def cleanup(self, request: CleanupRequest) -> CleanupResult:
        self.cleanups.append(request)
        return CleanupResult(
            run_id=request.run_id,
            fencing_token=request.fencing_token,
            idempotency_key=request.idempotency_key,
            succeeded=self.cleanup_succeeds,
            effect_ended_at=self.clock.now() if self.cleanup_succeeds else None,
            reason=None if self.cleanup_succeeds else "fake cleanup failure",
        )

    def transition_cleanup(self, request: CleanupRequest) -> CleanupResult:
        self.transition_cleanups.append(request)
        effect_ended_at = self.clock.now()
        self.clock.advance(self.transition_gap_sec)
        return CleanupResult(
            run_id=request.run_id,
            fencing_token=request.fencing_token,
            idempotency_key=request.idempotency_key,
            succeeded=self.cleanup_succeeds,
            effect_ended_at=effect_ended_at if self.cleanup_succeeds else None,
            reason=None if self.cleanup_succeeds else "fake transition cleanup failure",
        )


def _condition(signal: str, operator: str, value, *, ticks: int = 1) -> ConditionSet:
    return ConditionSet(
        conditions=[Condition(id=f"{signal.replace('_', '-')}-condition", signal=signal, operator=operator, value=value)],
        consecutive_ticks=ticks,
    )


def _spec(*, mode: AdaptiveMode = AdaptiveMode.CALIBRATION) -> ControllerSpec:
    levels = [
        AdaptiveLevel(
            id="low", parameters={"target_rps": 60}, min_hold_sec=20, settle_sec=10, timeout_sec=100
        ),
        AdaptiveLevel(
            id="high", parameters={"target_rps": 80}, min_hold_sec=10, settle_sec=5, timeout_sec=100
        ),
    ]
    if mode == AdaptiveMode.EVALUATION:
        levels = [levels[1]]
    return ControllerSpec(
        tick_interval_sec=10,
        max_injection_duration_sec=300,
        adaptive=AdaptiveSpec(
            mode=mode,
            levels=levels,
            success=_condition("achieved", "gte", 90, ticks=2),
            escalate=_condition("achieved", "lt", 90, ticks=2) if mode == AdaptiveMode.CALIBRATION else None,
            abort=_condition("entry", "ne", 200),
            must_rule_out=_condition("other_lock", "gt", 0),
        ),
        preflight_checks=["canonical-kubeconfig", "global-dirty-lease"],
        baseline=BaselinePlan(clean_window_sec=7200, checks=["baseline-process-active", "no-overlap"]),
        adapter_queries=[
            AdapterQuery(id="achieved", adapter="loadgen_summary", query_id="loadgen.achieved_rps", freshness_sec=30),
            AdapterQuery(id="entry", adapter="http_probe", query_id="http.entry_health", freshness_sec=15),
            AdapterQuery(
                id="other_lock", adapter="database",
                query_id="database.tagged_session_count", freshness_sec=30,
                parameters={"scenario_tag": "rca-F01-R-inventory-lock"},
            ),
        ],
        cleanup=CleanupPlan(),
        recovery=RecoveryPlan(conditions=_condition("entry", "eq", 200), timeout_sec=60),
        capture=CapturePlan(
            model_snapshot=(
                "/var/lib/lucida/ai-models/stream-anomaly/global/v1/model.json"
            )
        ),
    )


def _poller(
    clock: FakeClock,
    values: dict[str, object],
    *,
    stale_achieved: bool = False,
    fail_first: bool = False,
) -> ObservationPoller:
    classes = {
        "loadgen_summary": LoadgenSummaryAdapter,
        "http_probe": HttpProbeAdapter,
        "prometheus": PrometheusAdapter,
        "kubernetes": KubernetesAdapter,
        "database": DatabaseAdapter,
        "host_probe": HostProbeAdapter,
        "business_probe": BusinessProbeAdapter,
        "capture_status": CaptureStatusAdapter,
        "clickhouse": ClickHouseAdapter,
    }

    calls: dict[str, int] = {}

    def reader(query):
        calls[query.query_id] = calls.get(query.query_id, 0) + 1
        observed_at = clock.now()
        if stale_achieved and query.query_id == "loadgen.achieved_rps":
            observed_at -= timedelta(minutes=5)
        return {
            "value": values.get(query.query_id, True),
            "observed_at": observed_at,
            "source": f"fake:{query.adapter}",
            "quality": "error" if fail_first and calls[query.query_id] == 1 else "good",
        }

    adapters = {name: cls(reader, clock=clock.now) for name, cls in classes.items()}
    return ObservationPoller(ApprovedQueryRegistry.from_path(), adapters)


def _runtime(
    clock: FakeClock,
    values: dict[str, object],
    *,
    spec: ControllerSpec | None = None,
    eligibility: FakeEligibility | None = None,
    applier: FakeApplier | None = None,
) -> tuple[AdaptiveRuntime, FakeEligibility, FakeApplier]:
    check = eligibility or FakeEligibility(clock)
    profile = applier or FakeApplier(clock)
    controller = spec or _spec()
    runtime = AdaptiveRuntime.create(
        run_id="run-1",
        scenario_id="F07-H",
        fencing_token=7,
        profile_id="load.north_south.v1",
        approved_profile_id="load.north_south.v1" if controller.adaptive.mode == AdaptiveMode.EVALUATION else None,
        spec=controller,
        clock=clock,
        eligibility_probe=check,
        poller=_poller(clock, values),
        applier=profile,
    )
    return runtime, check, profile


async def test_baseline_eligibility_blocks_before_first_profile_apply() -> None:
    clock = FakeClock()
    eligibility = FakeEligibility(clock, baseline_active=False)
    runtime, _, applier = _runtime(clock, {}, eligibility=eligibility)

    session = await runtime.begin()

    assert session.status == SessionStatus.BLOCKED
    assert session.blocked_reasons == ["baseline_inactive"]
    assert not applier.applies
    assert not applier.cleanups
    assert eligibility.requests[0].clean_window_sec == 7200


async def test_eligibility_probe_may_complete_after_request_time() -> None:
    clock = FakeClock()
    eligibility = DelayedEligibility(clock)
    runtime, _, applier = _runtime(clock, {}, eligibility=eligibility)

    session = await runtime.begin()

    assert session.status == SessionStatus.ACTIVE
    assert session.blocked_reasons == []
    assert len(applier.applies) == 1


class ErringEligibility(FakeEligibility):
    """Fails checks by exception for the first ``failures`` inspections.

    Mirrors the F08-G 2026-08-03 batch shape: the probe raised, LiveProbes
    recorded a False with the exception preserved in check_errors, and a plain
    rerun passed.
    """

    def __init__(self, clock: FakeClock, *, failures: int = 1) -> None:
        super().__init__(clock)
        self.failures = failures

    def inspect(self, request):
        evidence = super().inspect(request)
        if len(self.requests) > self.failures:
            return evidence
        failing = request.checks[0]
        return evidence.model_copy(
            update={
                "check_results": {**evidence.check_results, failing: False},
                "check_errors": {failing: "LiveProbeError: transient transport failure"},
            }
        )


async def test_probe_exception_block_is_retried_once_before_blocking() -> None:
    clock = FakeClock()
    eligibility = ErringEligibility(clock, failures=1)
    runtime, _, applier = _runtime(clock, {}, eligibility=eligibility)

    session = await runtime.begin()

    assert session.status == SessionStatus.ACTIVE
    assert session.blocked_reasons == []
    assert len(eligibility.requests) == 2
    assert len(applier.applies) == 1


async def test_persistent_probe_exception_blocks_with_the_reason_preserved() -> None:
    clock = FakeClock()
    eligibility = ErringEligibility(clock, failures=2)
    runtime, _, applier = _runtime(clock, {}, eligibility=eligibility)

    session = await runtime.begin()

    assert session.status == SessionStatus.BLOCKED
    assert len(eligibility.requests) == 2
    failing = eligibility.requests[0].checks[0]
    assert f"check_failed:{failing}" in session.blocked_reasons
    assert (
        f"check_error:{failing}:LiveProbeError: transient transport failure"
        in session.blocked_reasons
    )
    assert not applier.applies


async def test_genuinely_unmet_check_blocks_without_a_retry() -> None:
    clock = FakeClock()
    eligibility = FakeEligibility(clock, baseline_active=False)
    runtime, _, _ = _runtime(clock, {}, eligibility=eligibility)

    session = await runtime.begin()

    assert session.status == SessionStatus.BLOCKED
    # A genuine negative is an answer, not an error — asking again would not
    # change it, so exactly one inspection happens.
    assert len(eligibility.requests) == 1


async def test_calibration_escalates_after_fresh_consecutive_ticks_and_cleans_up() -> None:
    clock = FakeClock()
    values = {
        "loadgen.achieved_rps": 50,
        "http.entry_health": 200,
        "database.tagged_session_count": 0,
    }
    runtime, _, applier = _runtime(clock, values)
    applier.transition_gap_sec = 1
    await runtime.begin()
    assert len(applier.applies) == 1

    clock.advance(20)
    await runtime.tick()
    assert len(applier.applies) == 1
    # The gate reads loadgen_summary, a 30s rolling window: a second confirmation
    # has to sit a full window later or it re-reads the sample that produced the
    # first one. Ticking again at +10s deliberately does not escalate.
    clock.advance(10)
    await runtime.tick()
    assert len(applier.applies) == 1
    clock.advance(20)
    session = await runtime.tick()
    assert session.controller_state and session.controller_state.level_id == "high"
    assert len(applier.applies) == 2
    assert len(applier.transition_cleanups) == 1
    assert session.level_changes[0].effect_ended_at == START + timedelta(seconds=50)
    assert session.level_changes[1].applied_at == START + timedelta(seconds=51)

    values["loadgen.achieved_rps"] = 100
    clock.advance(10)
    await runtime.tick()
    clock.advance(30)
    session = await runtime.tick()

    assert session.status == SessionStatus.CLEAN
    assert session.controller_state and session.controller_state.phase == ControllerPhase.SUCCEEDED
    assert len(applier.cleanups) == 1
    assert session.t1 == START
    assert session.t2 == clock.now()
    evidence = session.trusted_evidence()
    assert evidence["t1"] == "2026-07-16T08:00:00Z"
    assert evidence["t2"] == "2026-07-16T08:01:31Z"
    assert evidence["cleanup"] == {"status": "succeeded"}
    assert evidence["recovery"] == {"status": "succeeded"}


async def test_stale_decision_signal_cannot_escalate_and_forces_cleanup() -> None:
    clock = FakeClock()
    values = {
        "loadgen.achieved_rps": 50,
        "http.entry_health": 200,
        "database.tagged_session_count": 0,
    }
    runtime, _, applier = _runtime(clock, values)
    runtime.poller = _poller(clock, values, stale_achieved=True)
    await runtime.begin()
    clock.advance(100)

    session = await runtime.tick()

    assert session.status == SessionStatus.CLEAN
    assert session.controller_state and session.controller_state.phase == ControllerPhase.ABORTED
    assert session.controller_state.reason == "decision_observation_unavailable"
    assert len(applier.applies) == 1
    assert len(applier.cleanups) == 1


async def test_transient_probe_error_is_retried_once_before_controller_decision() -> None:
    clock = FakeClock()
    values = {
        "loadgen.achieved_rps": 100,
        "http.entry_health": 200,
        "database.tagged_session_count": 0,
    }
    runtime, _, _ = _runtime(clock, values)
    runtime.poller = _poller(clock, values, fail_first=True)
    await runtime.begin()
    clock.advance(20)

    session = await runtime.tick()

    assert session.controller_state is not None
    assert session.controller_state.reason == "conditions_pending"
    assert session.controller_state.streaks["success"] == 1


def test_evaluation_requires_the_approved_profile_and_one_level() -> None:
    clock = FakeClock()
    spec = _spec(mode=AdaptiveMode.EVALUATION)
    with pytest.raises(ValueError, match="exactly approved fixed profile"):
        AdaptiveRuntime.create(
            run_id="run-1",
            scenario_id="F07-H",
            fencing_token=7,
            profile_id="unapproved",
            approved_profile_id="approved",
            spec=spec,
            clock=clock,
            eligibility_probe=FakeEligibility(clock),
            poller=_poller(clock, {}),
            applier=FakeApplier(clock),
        )


async def test_recovery_timeout_marks_dirty_after_successful_cleanup() -> None:
    clock = FakeClock()
    values = {
        "loadgen.achieved_rps": 100,
        "http.entry_health": 200,
        "database.tagged_session_count": 0,
    }
    runtime, _, applier = _runtime(clock, values)
    await runtime.begin()
    values["http.entry_health"] = 503
    clock.advance(20)
    session = await runtime.tick()
    assert session.status == SessionStatus.RECOVERING
    assert session.cleanup and session.cleanup.succeeded
    assert len(applier.cleanups) == 1

    clock.advance(61)
    session = await runtime.tick()
    assert session.status == SessionStatus.DIRTY
    assert session.recovery and session.recovery.status == "failed"
    assert session.controller_state and session.controller_state.reason == "recovery_timeout"


async def test_run_whose_injection_never_applied_skips_the_recovery_gate() -> None:
    # The recovery conditions describe the aftermath of an injection, so a run
    # whose apply() was refused can never satisfy them: before this, such a run
    # sat in RECOVERING for the full timeout and then went DIRTY, blocking the
    # queue and burying the executor's refusal — the actual finding — under a
    # generic recovery_timeout. Seen across F03-H, F15-R and F05-P in the
    # 2026-08-03 batch.
    class RefusingApplier(FakeApplier):
        def apply(self, request: ApplyRequest) -> ApplyResult:
            raise RuntimeError("profile control refused: parameters are not approved")

    clock = FakeClock()
    values = {
        "loadgen.achieved_rps": 100,
        "http.entry_health": 200,
        "database.tagged_session_count": 0,
    }
    applier = RefusingApplier(clock)
    runtime, _, _ = _runtime(clock, values, applier=applier)
    await runtime.begin()

    session = runtime.session
    assert session.level_changes == []
    assert session.cleanup and session.cleanup.succeeded
    # Straight to CLEAN, with the reason recorded rather than a timeout.
    assert session.status == SessionStatus.CLEAN
    assert session.recovery and session.recovery.status == "succeeded"
    assert session.recovery.reason == "no_injection_applied"

    # And it must not linger: advancing past the recovery timeout changes nothing.
    clock.advance(600)
    session = await runtime.tick()
    assert session.status == SessionStatus.CLEAN


async def test_cleanup_failure_marks_dirty_and_session_restores_without_reapply() -> None:
    clock = FakeClock()
    values = {
        "loadgen.achieved_rps": 100,
        "http.entry_health": 200,
        "database.tagged_session_count": 0,
    }
    applier = FakeApplier(clock, cleanup_succeeds=False)
    runtime, eligibility, _ = _runtime(clock, values, applier=applier)
    await runtime.begin()
    clock.advance(20)
    await runtime.tick()
    # +30s: the second confirmation must read a new loadgen window (see the
    # escalation test above).
    clock.advance(30)
    session = await runtime.tick()

    assert session.status == SessionStatus.DIRTY
    assert session.controller_state and session.controller_state.phase == ControllerPhase.DIRTY
    assert len(applier.applies) == 1
    assert len(applier.cleanups) == 1
    restored = AdaptiveRuntime.restore(
        session.model_dump_json(),
        clock=clock,
        eligibility_probe=eligibility,
        poller=_poller(clock, values),
        applier=applier,
    )
    same = await restored.tick()
    assert ControllerSession.model_validate_json(same.model_dump_json()) == session
    assert len(applier.applies) == 1
    assert len(applier.cleanups) == 1


async def test_tick_snapshot_exposes_polled_signals_for_diagnostics() -> None:
    clock = FakeClock()
    values = {
        "loadgen.achieved_rps": 95,
        "http.entry_health": 200,
        "database.tagged_session_count": 0,
    }
    runtime, _, _ = _runtime(clock, values)
    await runtime.begin()
    assert runtime.last_tick is None

    clock.advance(10)
    await runtime.tick()

    tick = runtime.last_tick
    assert tick is not None
    assert tick["elapsed_sec"] == 10
    assert tick["signals"]["achieved"]["value"] == 95
    assert tick["signals"]["achieved"]["usable"] is True
    assert tick["signals"]["entry"]["quality"] == "good"


async def test_slightly_future_observed_at_is_not_marked_stale() -> None:
    """Remote-store evaluation timestamps may sit ahead of the tick clock.

    F03-G run ca64d788 (07-19): VictoriaMetrics stamped observed_at up to 1s
    after the tick's reference now, so healthy p95 readings were marked stale
    on 14/31 hold ticks and the success streak never reached 3.
    """
    clock = FakeClock()
    values = {
        "loadgen.achieved_rps": 100,
        "http.entry_health": 200,
        "database.tagged_session_count": 0,
    }
    runtime, _, _ = _runtime(clock, values)
    await runtime.begin()

    original_poll = runtime.poller.poll

    async def future_stamped(queries):
        result = await original_poll(queries)
        return {
            key: value.model_copy(
                update={"observed_at": value.observed_at + timedelta(seconds=5)}
            )
            for key, value in result.items()
        }

    runtime.poller.poll = future_stamped
    clock.advance(10)
    await runtime.tick()

    tick = runtime.last_tick
    assert tick is not None
    for name, signal in tick["signals"].items():
        assert signal["freshness"] == "fresh", name
        assert signal["usable"] is True, name


def _isolation_evidence(now):
    # A dirty clean-window (check_results false) plus a lingering overlap — the
    # exact shape a prior cycle run leaves inside the 30m overlap window.
    return EligibilityEvidence(
        checked_at=now,
        source="fake:baseline-store",
        quality="good",
        check_results={"clean-window": False, "baseline-traffic": True},
        clean_window_start=now - timedelta(minutes=5),  # too short for v2
        clean_window_end=now,
        overlapping_run_ids=["prev-cycle-run"],
        baseline_active=True,
    )


def test_v2_isolation_gates_block_on_overlap_and_clean_window():
    from app.adaptive_runtime import EligibilityRequest, _eligibility_reasons

    now = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
    request = EligibilityRequest(
        run_id="r", scenario_id="F01-H",
        checks=["clean-window", "baseline-traffic"],
        clean_window_sec=1800, requested_at=now,
    )
    reasons = _eligibility_reasons(request, _isolation_evidence(now))
    assert "check_failed:clean-window" in reasons
    assert "scenario_overlap" in reasons
    assert "clean_window_too_short" in reasons


def test_cycle_skip_isolation_suppresses_clean_window_and_overlap():
    from app.adaptive_runtime import EligibilityRequest, _eligibility_reasons

    now = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
    request = EligibilityRequest(
        run_id="r", scenario_id="F01-H",
        checks=["clean-window", "baseline-traffic"],
        clean_window_sec=1800, requested_at=now,
    )
    reasons = _eligibility_reasons(
        request, _isolation_evidence(now), skip_isolation_checks=True
    )
    assert "check_failed:clean-window" not in reasons
    assert "scenario_overlap" not in reasons
    assert "clean_window_too_short" not in reasons
    assert "clean_window_after_check" not in reasons
    # Non-isolation gates still apply.
    assert reasons == []


def test_skip_isolation_still_enforces_baseline_active():
    from app.adaptive_runtime import EligibilityRequest, _eligibility_reasons

    now = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)
    request = EligibilityRequest(
        run_id="r", scenario_id="F01-H",
        checks=["baseline-traffic"], clean_window_sec=1800, requested_at=now,
    )
    evidence = _isolation_evidence(now).model_copy(update={"baseline_active": False})
    reasons = _eligibility_reasons(request, evidence, skip_isolation_checks=True)
    assert "baseline_inactive" in reasons
