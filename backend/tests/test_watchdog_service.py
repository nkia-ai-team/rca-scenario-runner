from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.coordinator import GlobalCoordinator
from app.watchdog_service import WatchdogService


NOW = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)


class Clock:
    def now(self):
        return NOW


def setup_expired(tmp_path):
    coordinator = GlobalCoordinator(tmp_path / "coordinator.json")
    lease = coordinator.acquire(
        run_id="run-expired",
        scenario_id="F07-H",
        now=NOW - timedelta(minutes=2),
        lease_sec=10,
    )
    contract = tmp_path / "contracts"
    contract.mkdir()
    plan = {
        "scenario": {"id": "F07-H", "slug": "f07-h-north-south-surge"},
        "live_allowed": True,
        "plan_digest": "a" * 64,
        "profile_instances": [],
    }
    dispatcher = contract / "run-scenario.sh"
    dispatcher.write_text(
        "#!/bin/sh\nprintf '%s\\n' '" + json.dumps({"normalized_plan": plan}) + "'\n",
        encoding="utf-8",
    )
    dispatcher.chmod(0o755)
    profile_control = contract / "profile-control.py"
    profile_control.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    profile_control.chmod(0o755)
    from app.production_runtime import RunArtifactStore

    RunArtifactStore(tmp_path / "runs").prepare_capsule(
        lease.run_id,
        contract_root=contract,
        scenario_slug="f07-h-north-south-surge",
        binding={
            "catalog_slug": "f07-h-north-south-surge",
            "primary_profile": "load.north_south",
            "logical_profile_id": "load.north_south",
            "companion_profiles": [],
        },
    )
    return coordinator, lease


async def test_expired_lease_cleanup_runs_once_and_restart_is_idle(tmp_path) -> None:
    coordinator, _ = setup_expired(tmp_path)
    calls = []

    class Applier:
        def cleanup(self, request):
            calls.append(request)
            return SimpleNamespace(succeeded=True, reason=None)

    service = WatchdogService(
        coordinator,
        tmp_path / "runs",
        clock=Clock(),
        applier_factory=lambda *args, **kwargs: Applier(),
    )
    assert (await service.tick()).reason == "watchdog_cleanup_recovered"
    assert (await WatchdogService(coordinator, tmp_path / "runs", clock=Clock()).tick()).action == "idle"
    assert len(calls) == 1


async def test_watchdog_race_loses_claim_without_duplicate_cleanup(tmp_path) -> None:
    coordinator, lease = setup_expired(tmp_path)
    coordinator.claim_cleanup(
        run_id=lease.run_id,
        fencing_token=lease.fencing_token,
        claimant="other-watchdog",
        now=NOW,
    )
    service = WatchdogService(
        coordinator,
        tmp_path / "runs",
        clock=Clock(),
        applier_factory=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()),
    )
    assert (await service.tick()).action == "cleanup_claimed"
    try:
        coordinator.heartbeat(
            run_id=lease.run_id,
            fencing_token=lease.fencing_token,
            now=NOW,
            lease_sec=60,
        )
    except RuntimeError as error:
        assert "terminal fence" in str(error)
    else:
        raise AssertionError("heartbeat renewed a cleanup-claimed lease")


async def test_heartbeat_renewal_between_decision_and_claim_prevents_cleanup(tmp_path) -> None:
    class RenewingCoordinator(GlobalCoordinator):
        def claim_expired_cleanup(self, **kwargs):
            lease = self.snapshot().active_lease
            assert lease is not None
            self.heartbeat(
                run_id=lease.run_id,
                fencing_token=lease.fencing_token,
                now=NOW,
                lease_sec=60,
            )
            return super().claim_expired_cleanup(**kwargs)

    base, _ = setup_expired(tmp_path)
    coordinator = RenewingCoordinator(base.state_path)
    service = WatchdogService(
        coordinator,
        tmp_path / "runs",
        clock=Clock(),
        applier_factory=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError()),
    )
    decision = await service.tick()
    assert decision.action == "healthy"
    assert coordinator.snapshot().cleanup_claim is None


async def test_cleanup_failure_or_plan_drift_marks_dirty_and_blocks_restart(tmp_path) -> None:
    coordinator, lease = setup_expired(tmp_path)

    class Applier:
        def cleanup(self, request):
            return SimpleNamespace(succeeded=False, reason="plan digest drift")

    service = WatchdogService(
        coordinator,
        tmp_path / "runs",
        clock=Clock(),
        applier_factory=lambda *args, **kwargs: Applier(),
    )
    decision = await service.tick()
    assert decision.action == "block_dirty"
    dirty = coordinator.snapshot().dirty_run
    assert dirty and dirty.run_id == lease.run_id and dirty.reason == "plan digest drift"
    assert (await WatchdogService(coordinator, tmp_path / "runs", clock=Clock()).tick()).action == "block_dirty"
