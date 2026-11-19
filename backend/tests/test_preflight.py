from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.preflight import (
    AiJudgement,
    PreflightCheckInput,
    evaluate_preflight,
)

NOW = datetime(2026, 7, 16, 1, 0, 0, tzinfo=timezone.utc)
WINDOW = ("2026-07-16T00:50:00Z", "2026-07-16T01:00:00Z")


def _r6_checks(*, five_xx: float = 0.0, p95: float = 0.4, pool_pending: float = 0.0):
    # A representative R6 deterministic check set for the [t1-10m, t1] window.
    return [
        PreflightCheckInput(name="baseline_loadgen_alive", value=1, threshold=1, op="gte"),
        PreflightCheckInput(name="diurnal_band_rps", value=8, threshold=5, op="gte"),
        PreflightCheckInput(name="user_5xx_rate", value=five_xx, threshold=0.01, op="lte", tolerance=0.01),
        PreflightCheckInput(name="entry_p95_sec", value=p95, threshold=0.5, op="lte"),
        PreflightCheckInput(name="active_alarms", value=0, threshold=0, op="eq"),
        PreflightCheckInput(name="open_incidents", value=0, threshold=0, op="eq"),
        PreflightCheckInput(name="prev_pool_pending", value=pool_pending, threshold=0, op="eq"),
    ]


class RecordingJudge:
    def __init__(self, clean: bool) -> None:
        self.clean = clean
        self.calls = 0

    def adjudicate(self, *, window, checks):
        self.calls += 1
        return AiJudgement(clean=self.clean, rationale="borderline reviewed")


def test_all_pass_is_clean() -> None:
    verdict = evaluate_preflight(window=WINDOW, checks=_r6_checks(), now=NOW)
    assert verdict.verdict == "clean"
    assert verdict.is_clean
    assert verdict.checked_at == "2026-07-16T01:00:00Z"
    assert all(check.passed for check in verdict.checks)


def test_wait_promotes_to_clean_after_wait() -> None:
    verdict = evaluate_preflight(window=WINDOW, checks=_r6_checks(), now=NOW, waited_sec=300)
    assert verdict.verdict == "clean_after_wait"
    assert verdict.is_clean


def test_hard_failure_is_dirty_and_bypasses_ai() -> None:
    judge = RecordingJudge(clean=True)
    verdict = evaluate_preflight(
        window=WINDOW,
        checks=_r6_checks(p95=5.0),  # far past threshold+tolerance -> hard fail
        now=NOW,
        ai_judge=judge,
    )
    assert verdict.verdict == "dirty"
    assert not verdict.is_clean
    assert judge.calls == 0


def test_borderline_without_ai_is_dirty() -> None:
    # 5xx 0.015 fails the 0.01 threshold but is within 0.01 tolerance -> borderline.
    verdict = evaluate_preflight(window=WINDOW, checks=_r6_checks(five_xx=0.015), now=NOW)
    assert verdict.verdict == "dirty"
    assert any(check.status == "borderline" for check in verdict.checks)


def test_borderline_cleared_by_ai_is_ai_judged_clean() -> None:
    judge = RecordingJudge(clean=True)
    verdict = evaluate_preflight(
        window=WINDOW, checks=_r6_checks(five_xx=0.015), now=NOW, ai_judge=judge
    )
    assert verdict.verdict == "ai_judged_clean"
    assert verdict.is_clean
    assert judge.calls == 1
    assert verdict.ai_judgement is not None and verdict.ai_judgement.clean


def test_borderline_rejected_by_ai_is_dirty() -> None:
    judge = RecordingJudge(clean=False)
    verdict = evaluate_preflight(
        window=WINDOW, checks=_r6_checks(five_xx=0.015), now=NOW, ai_judge=judge
    )
    assert verdict.verdict == "dirty"
    assert judge.calls == 1


def test_to_meta_matches_capture_contract() -> None:
    verdict = evaluate_preflight(window=WINDOW, checks=_r6_checks(), now=NOW, waited_sec=0)
    meta = verdict.to_meta()
    assert set(meta) == {"window", "verdict", "checked_at", "waited_sec", "checks", "ai_judgement"}
    assert meta["window"] == list(WINDOW)
    assert meta["ai_judgement"] is None
    first = meta["checks"][0]
    assert set(first) == {"name", "value", "threshold", "pass"}
    assert first["pass"] is True


def test_naive_clock_and_empty_checks_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        evaluate_preflight(window=WINDOW, checks=_r6_checks(), now=datetime(2026, 7, 16, 1, 0, 0))
    with pytest.raises(ValueError, match="at least one check"):
        evaluate_preflight(window=WINDOW, checks=[], now=NOW)


def test_excused_alarms_join_the_record_only_when_decoys_were_in_play() -> None:
    """U7: meta.json gains a key, existing keys keep their shape and values."""
    plain = evaluate_preflight(window=WINDOW, checks=_r6_checks(), now=NOW).to_meta()
    excused = [{"decoy_id": "d1", "labels": {"service_name": "s"}, "active_at": "t"}]
    with_decoys = evaluate_preflight(
        window=WINDOW, checks=_r6_checks(), now=NOW, excused_alarms=excused
    ).to_meta()

    assert set(plain) == {"window", "verdict", "checked_at", "waited_sec", "checks", "ai_judgement"}
    assert with_decoys == {**plain, "excused_alarms": excused}


# --- VM testbed (civil-petition) signal set: testbed-services docs/spec-testbed-vm-3tier.md §6-4 ---------------

VM_CLEAN = {
    "baseline_loadgen_alive": 1,
    "achieved_rps": 17.0,
    "diurnal_rps_floor": 8.0,
    "user_5xx_rate": 0.0,
    "entry_p95_sec": 0.009,
    "active_alarms": 0,
    "open_incidents": 0,
    "residual_guards": 0,
    "tunnel_handshake_age_sec": 40,
    "tunnel_ping_lost": 0,
    "tunnel_ping_max_ms": 1.8,
    "tunnel_load_mbps": 0.6,
    "sms_cpu_sample_age_sec": 12,
    "sms_icmp_down": 0,
    "sms_icmp_sample_age_sec": 20,
    "router_snmp_down": 0,
    "router_snmp_sample_age_sec": 40,
    "router_cpu_pct": 6.0,
    "mgmt_ssh_down": 0,
    "mgmt_ssh_settle_sec": 7200,
    "clock_offset_ms": 0.4,
}


def _vm_verdict(**overrides):
    from app.preflight import build_preflight_checks

    return evaluate_preflight(
        window=WINDOW, checks=build_preflight_checks({**VM_CLEAN, **overrides}, domain="civil-petition"), now=NOW
    )


def test_vm_domain_has_its_own_signal_set_and_keeps_r6_for_the_others() -> None:
    from app.preflight import R6_REQUIRED_SIGNALS, VM_DOMAIN_REQUIRED_SIGNALS, build_preflight_checks, required_signals

    assert required_signals(None) == required_signals("commerce") == R6_REQUIRED_SIGNALS
    assert required_signals("civil-petition") == VM_DOMAIN_REQUIRED_SIGNALS == frozenset(VM_CLEAN)
    # No commerce pool here; the previous run's residue is the armed guards.
    assert "prev_pool_pending" not in VM_DOMAIN_REQUIRED_SIGNALS
    verdict = _vm_verdict()
    assert verdict.verdict == "clean"
    names = [check.name for check in verdict.checks]
    assert names[:7] == [
        "baseline_loadgen_alive", "diurnal_band_rps", "user_5xx_rate", "entry_p95_sec",
        "active_alarms", "open_incidents", "residual_guards",
    ]
    with pytest.raises(ValueError, match="tunnel_handshake_age_sec"):
        build_preflight_checks({k: v for k, v in VM_CLEAN.items() if k != "tunnel_handshake_age_sec"}, domain="civil-petition")
    # The R6 set without a domain still refuses the VM observations (no silent cross-domain pass).
    with pytest.raises(ValueError, match="prev_pool_pending"):
        build_preflight_checks(VM_CLEAN)


@pytest.mark.parametrize(
    ("override", "failing"),
    [
        ({"tunnel_handshake_age_sec": 151}, "tunnel_handshake_age_sec"),
        ({"tunnel_ping_lost": 1}, "tunnel_ping_lost"),
        ({"tunnel_ping_max_ms": 20.5}, "tunnel_ping_max_ms"),
        ({"tunnel_load_mbps": 20.1}, "tunnel_load_mbps"),
        ({"sms_cpu_sample_age_sec": 91}, "sms_cpu_sample_age_sec"),
        ({"sms_icmp_down": 1}, "sms_icmp_down"),
        ({"sms_icmp_sample_age_sec": 91}, "sms_icmp_sample_age_sec"),
        ({"router_snmp_down": 1}, "router_snmp_down"),
        ({"router_snmp_sample_age_sec": 181}, "router_snmp_sample_age_sec"),
        ({"router_cpu_pct": 50.5}, "router_cpu_pct"),
        ({"mgmt_ssh_down": 1}, "mgmt_ssh_down"),
        ({"mgmt_ssh_settle_sec": 1799}, "mgmt_ssh_settle_sec"),
        ({"residual_guards": 1}, "residual_guards"),
        ({"clock_offset_ms": 50.1}, "clock_offset_ms"),
        ({"achieved_rps": 7.9}, "diurnal_band_rps"),
        ({"baseline_loadgen_alive": 0}, "baseline_loadgen_alive"),
    ],
)
def test_each_vm_domain_check_fails_the_gate_hard(override, failing) -> None:
    """Every §6-4 addition is a hard check (no tolerance band for the AI layer); the limits are the design's."""
    verdict = _vm_verdict(**override)
    assert verdict.verdict == "dirty"
    assert [check.name for check in verdict.checks if check.status != "pass"] == [failing]
    assert next(check for check in verdict.checks if check.name == failing).status == "fail"


def test_vm_domain_limits_hold_at_the_boundary() -> None:
    assert _vm_verdict(
        tunnel_handshake_age_sec=150, tunnel_ping_max_ms=20, tunnel_load_mbps=20, sms_cpu_sample_age_sec=90,
        sms_icmp_sample_age_sec=90, router_snmp_sample_age_sec=180, router_cpu_pct=50, mgmt_ssh_settle_sec=1800,
        clock_offset_ms=50,
    ).verdict == "clean"
    # The R6 abnormal-share and p95 bands keep their tolerance (borderline, AI layer may adjudicate).
    assert _vm_verdict(user_5xx_rate=0.02).verdict == "dirty"
    assert next(c for c in _vm_verdict(user_5xx_rate=0.02).checks if c.name == "user_5xx_rate").status == "borderline"
