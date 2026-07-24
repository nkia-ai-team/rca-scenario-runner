import asyncio
import inspect
import json
import os
import re
import uuid
from collections import deque
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Literal, Optional, Protocol

from app.execution import build_invocation, describe_invocation
from app.controller import normalized_controller_plan
from app.models import ActiveRun, ExecutionSpec, HistoryEntry, RunInfo, Status
from app.manifests import get_manifest, normalized_manifest_plan
from app.scenarios import get_scenario
from app.coordinator import GlobalCoordinator
from app.adaptive_runtime import AdaptiveRuntime, CleanupRequest, SessionStatus
from app.adaptive import ControllerAction, ControllerPhase
from app.capture_orchestration import (
    CaptureEvidence,
    CaptureRequest,
    CaptureScheduler,
    load_scenario_metadata,
)
from app.production_runtime import (
    TRUSTED_CAPTURE_SCRIPT,
    TRUSTED_CATALOG,
    TRUSTED_DISPATCHER,
    TRUSTED_SCENARIO_METADATA,
    ProductionCaptureInvoker,
    RunArtifactStore,
    SystemClock,
    TrustedDispatcherApplier,
    file_sha256,
    atomic_json,
    production_runtime,
)
from app.watchdog_service import WatchdogService

_LOG_TAIL_SIZE = 200
_HISTORY_SIZE = 20
_DEFAULT_LEASE_SEC = 30
_DEFAULT_HEARTBEAT_INTERVAL_SEC = 5
_CANONICAL_KUBECONFIG = "/root/tb-kubeconfig"
_CANONICAL_KUBE_CONTEXT = "kubernetes-admin@kubernetes"

RecoveryValidator = Callable[[object], bool | Awaitable[bool]]
RuntimeFactory = Callable[..., AdaptiveRuntime]
Sleeper = Callable[[float], Awaitable[None]]


class Clock(Protocol):
    def now(self) -> datetime: ...


def execution_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["KUBECONFIG"] = _CANONICAL_KUBECONFIG
    env["KUBE_CONTEXT"] = _CANONICAL_KUBE_CONTEXT
    return env


class ScenarioRunner:
    """
    Single-active-run runner. Only one scenario (or cleanup) may execute at a time.
    Process-local history is paired with a persistent global coordinator lease.
    """

    def __init__(
        self,
        script_dir: Path,
        log_dir: Path,
        *,
        coordinator: GlobalCoordinator | None = None,
        recovery_validator: RecoveryValidator | None = None,
        lease_sec: int = _DEFAULT_LEASE_SEC,
        heartbeat_interval_sec: float = _DEFAULT_HEARTBEAT_INTERVAL_SEC,
        runtime_factory: RuntimeFactory | None = None,
        artifact_store: RunArtifactStore | None = None,
        capture_scheduler: CaptureScheduler | None = None,
        clock: Clock | None = None,
        sleeper: Sleeper = asyncio.sleep,
        dispatcher_path: Path = TRUSTED_DISPATCHER,
        catalog_path: Path = TRUSTED_CATALOG,
        scenario_metadata_path: Path = TRUSTED_SCENARIO_METADATA,
        watchdog_service: WatchdogService | None = None,
    ) -> None:
        self.script_dir = script_dir
        self.log_dir = log_dir
        coordinator_path = Path(
            os.environ.get(
                "COORDINATOR_STATE_PATH",
                str(log_dir.parent / "state" / "coordinator.json"),
            )
        )
        self.coordinator = coordinator or GlobalCoordinator(coordinator_path)
        self.recovery_validator = recovery_validator
        self.lease_sec = lease_sec
        self.heartbeat_interval_sec = heartbeat_interval_sec
        self.clock = clock or SystemClock()
        self.sleeper = sleeper
        self.runtime_factory = runtime_factory
        self.artifact_store = artifact_store or RunArtifactStore(
            Path(os.environ.get("TRUSTED_RUNS_ROOT", "/var/lib/lucida/scenario-runs"))
        )
        self.dispatcher_path = dispatcher_path
        self.catalog_path = catalog_path
        self.scenario_metadata_path = scenario_metadata_path
        capture_state = Path(
            os.environ.get("CAPTURE_STATE_PATH", str(log_dir.parent / "state" / "capture-jobs.json"))
        )
        self.capture_scheduler = capture_scheduler or CaptureScheduler(
            capture_state,
            clock=self.clock,
            invoker=ProductionCaptureInvoker(
                runs_root=self.artifact_store.root,
                capture_script=Path(os.environ.get("CAPTURE_SCRIPT", str(TRUSTED_CAPTURE_SCRIPT))),
                output_root=Path(os.environ.get("EVAL_CASE_ROOT", "/data/eval-cases")),
                model_ssh_target=os.environ.get("MODEL_SNAPSHOT_SSH_TARGET"),
            ),
        )
        self.watchdog_service = watchdog_service or WatchdogService(
            self.coordinator,
            self.artifact_store.root,
            clock=self.clock,
            heartbeat_timeout_sec=self.lease_sec,
        )

        self._lock = asyncio.Lock()
        self._current: Optional[RunInfo] = None
        self._log_buffer: deque[str] = deque(maxlen=_LOG_TAIL_SIZE)
        self._history: deque[HistoryEntry] = deque(maxlen=_HISTORY_SIZE)
        self._task: Optional[asyncio.Task] = None
        self._capture_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None

    @property
    def is_busy(self) -> bool:
        return self._current is not None and self._current.status in {
            "running",
            "cleanup_running",
        }

    def get_current(self) -> Optional[RunInfo]:
        if self._current is None:
            return None
        return self._current.model_copy(update={"log_tail": list(self._log_buffer)})

    def get_active(self) -> ActiveRun:
        """Snapshot for /api/active — what (if anything) the runner is busy with right now."""
        if not self.is_busy or self._current is None:
            return ActiveRun(is_active=False)
        return ActiveRun(
            is_active=True,
            scenario_id=self._current.scenario_id,
            run_id=self._current.run_id,
            mode=self._current.mode,
            started_at=self._current.started_at,
            fencing_token=self._current.fencing_token,
        )

    def get_history(self) -> list[HistoryEntry]:
        return list(reversed(self._history))

    def log_path(self, run_id: str) -> Path:
        return self.log_dir / f"{run_id}.log"

    def _resolve_script_path(self, scenario) -> Path:
        multi = self.script_dir / scenario.domain / "scripts" / scenario.script_filename
        flat = self.script_dir / scenario.script_filename
        if multi.exists():
            return multi
        if flat.exists():
            return flat
        raise FileNotFoundError(f"Script not found: tried {multi} and {flat}")

    def dry_run(
        self,
        scenario_id: str,
        mode: Literal["run", "cleanup"] = "run",
    ) -> dict:
        """Compile a plan without subprocesses, remote access, logs, or state changes."""
        scenario = get_scenario(scenario_id)
        if scenario is None:
            manifest = get_manifest(scenario_id)
            if manifest is None:
                raise ValueError(f"Unknown scenario: {scenario_id}")
            plan = normalized_manifest_plan(manifest, mode=mode)
            kube_preflight = self._kubernetes_preflight_plan()
            plan["valid"] = all(check["passed"] for check in kube_preflight["checks"])
            plan["coordinator"] = self._coordinator_plan()
            plan["kubernetes_preflight"] = kube_preflight
            return plan
        script_path = self._resolve_script_path(scenario)
        points = []
        for point in scenario.execution.injection_points:
            points.append(
                {
                    "id": point.id,
                    "kind": point.kind,
                    "transport": point.transport,
                    "location": point.location,
                    "target": point.target,
                    "entry_path": point.entry_path,
                    "cleanup_location": point.cleanup_location,
                    "feasibility": point.feasibility,
                    "managed_by": point.managed_by,
                    "required_secret_env": sorted(set(point.header_env.values())),
                }
            )
        kube_preflight = self._kubernetes_preflight_plan()
        return {
            "valid": all(check["passed"] for check in kube_preflight["checks"]),
            "side_effects": False,
            "scenario_id": scenario.id,
            "mode": mode,
            "orchestrator": describe_invocation(
                scenario.execution.orchestrator,
                script_path,
                scenario.id,
                mode,
            ),
            "injection_points": points,
            "controller": normalized_controller_plan(
                scenario.controller,
                scenario_id=scenario.id,
            ),
            "coordinator": self._coordinator_plan(),
            "kubernetes_preflight": kube_preflight,
            "warnings": [
                "dry-run does not contact SSH, Docker, Kubernetes, or API targets",
                *(
                    ["legacy scenario has no explicit injection_points"]
                    if not points
                    else []
                ),
            ],
        }

    async def start(
        self,
        scenario_id: str,
        mode: Literal["run", "cleanup"],
        *,
        skip_isolation_checks: bool = False,
    ) -> RunInfo:
        scenario = get_scenario(scenario_id)
        external_manifest = None
        if scenario is None:
            external_manifest = get_manifest(scenario_id)
            if external_manifest is None:
                raise ValueError(f"Unknown scenario: {scenario_id}")
            scenario = external_manifest.runtime_scenario()
            if scenario is None:
                raise RuntimeError(
                    f"external scenario is plan-only or unresolved: {external_manifest.id}"
                )
            if mode == "cleanup":
                return await self._start_external_dirty_cleanup(scenario)

        if self._lock.locked() or self.is_busy:
            raise RuntimeError("Another scenario is already running")

        if mode == "run" and external_manifest is not None:
            load_scenario_metadata(self.scenario_metadata_path, scenario.id)

        script_path = (
            self.dispatcher_path
            if external_manifest is not None
            else self._resolve_script_path(scenario)
        )
        run_id = f"{scenario.id}-{mode}-{uuid.uuid4().hex[:8]}"
        now = self.clock.now()
        adaptive_enabled = (
            scenario.controller is not None
            and isinstance(scenario.injection, dict)
            and isinstance(scenario.injection.get("profile_id"), str)
        )
        run_dir = None
        prepared_plan = None
        if adaptive_enabled and mode == "run" and self.runtime_factory is None:
            logical_profile = (
                scenario.injection.get("approved_profile_id")
                if scenario.controller.adaptive.mode.value == "evaluation"
                else scenario.injection.get("profile_id")
            )
            binding = {
                "catalog_slug": scenario.injection.get("catalog_slug"),
                "primary_profile": scenario.injection.get("profile_id"),
                "logical_profile_id": logical_profile,
                "companion_profiles": scenario.injection.get("companion_profile_ids", []),
            }
            run_dir, prepared_plan = self.artifact_store.prepare_capsule(
                run_id,
                contract_root=self.dispatcher_path.parent,
                scenario_slug=scenario.injection["catalog_slug"],
                binding=binding,
            )
            if prepared_plan["scenario"]["id"] != scenario.id:
                raise RuntimeError("compiled capsule plan scenario id does not match manifest")
        manual_dirty_cleanup = False
        lease_run_id = run_id
        snapshot = self.coordinator.snapshot()
        if mode == "cleanup" and snapshot.dirty_run is not None:
            dirty = snapshot.dirty_run
            if dirty.scenario_id != scenario.id:
                raise RuntimeError(f"dirty run blocks cleanup for another scenario: {dirty.run_id}")
            self.coordinator.claim_cleanup(
                run_id=dirty.run_id,
                fencing_token=dirty.fencing_token,
                claimant=run_id,
                now=now,
            )
            manual_dirty_cleanup = True
            lease_run_id = dirty.run_id
            lease = dirty
        else:
            lease = self.coordinator.acquire(
                run_id=run_id,
                scenario_id=scenario.id,
                now=now,
                lease_sec=self.lease_sec,
            )
            if run_dir is not None:
                self.artifact_store.bind_lease(
                    run_dir, run_id=run_id, fencing_token=lease.fencing_token
                )
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            if adaptive_enabled and mode == "run":
                if self.runtime_factory is not None:
                    plan = {
                        "schema_version": 1,
                        "scenario_id": scenario.id,
                        "profile_id": scenario.injection.get("profile_id"),
                        "approved_profile_id": scenario.injection.get("approved_profile_id"),
                        "companion_profile_ids": scenario.injection.get("companion_profile_ids", []),
                        "catalog_slug": scenario.injection.get("catalog_slug"),
                        "controller": normalized_controller_plan(
                            scenario.controller, scenario_id=scenario.id
                        ),
                    }
                    logical_profile = (
                        scenario.injection.get("approved_profile_id")
                        if scenario.controller.adaptive.mode.value == "evaluation"
                        else scenario.injection.get("profile_id")
                    )
                    plan["controller_runtime"] = {
                        "catalog_slug": scenario.injection.get("catalog_slug"),
                        "primary_profile": scenario.injection.get("profile_id"),
                        "logical_profile_id": logical_profile,
                        "companion_profiles": scenario.injection.get("companion_profile_ids", []),
                    }
                    run_dir = self.artifact_store.create(run_id, plan)
        except Exception:
            if manual_dirty_cleanup:
                self.coordinator.finish_cleanup_claim(
                    run_id=lease_run_id,
                    fencing_token=lease.fencing_token,
                    claimant=run_id,
                )
            else:
                self.coordinator.release(
                    run_id=lease_run_id,
                    fencing_token=lease.fencing_token,
                )
            raise

        status: Status = "cleanup_running" if mode == "cleanup" else "running"
        self._current = RunInfo(
            run_id=run_id,
            scenario_id=scenario.id,
            mode=mode,
            status=status,
            started_at=now,
            fencing_token=lease.fencing_token,
        )
        self._log_buffer.clear()

        self._task = asyncio.create_task(
            self._execute(
                script_path=script_path,
                scenario_id=scenario.id,
                mode=mode,
                execution=scenario.execution.orchestrator,
                scenario=scenario,
                lease_run_id=lease_run_id,
                fencing_token=lease.fencing_token,
                manual_dirty_cleanup=manual_dirty_cleanup,
                run_dir=run_dir,
                skip_isolation_checks=skip_isolation_checks,
            )
        )
        return self.get_current()  # type: ignore[return-value]

    async def _start_external_dirty_cleanup(self, scenario) -> RunInfo:
        if self._lock.locked() or self.is_busy:
            raise RuntimeError("Another scenario is already running")
        dirty = self.coordinator.snapshot().dirty_run
        if dirty is None or dirty.scenario_id != scenario.id:
            raise RuntimeError("external cleanup requires a matching DIRTY run")
        claimant = f"manual-cleanup-{uuid.uuid4().hex[:8]}"
        self.coordinator.claim_cleanup(
            run_id=dirty.run_id,
            fencing_token=dirty.fencing_token,
            claimant=claimant,
            now=self.clock.now(),
        )
        self._current = RunInfo(
            run_id=claimant,
            scenario_id=scenario.id,
            mode="cleanup",
            status="cleanup_running",
            started_at=self.clock.now(),
            fencing_token=dirty.fencing_token,
        )
        self._task = asyncio.create_task(
            self._execute_external_dirty_cleanup(
                dirty.run_id, dirty.fencing_token, claimant
            )
        )
        return self.get_current()  # type: ignore[return-value]

    async def _execute_external_dirty_cleanup(
        self, dirty_run_id: str, fencing_token: int, claimant: str
    ) -> None:
        assert self._current is not None
        success = False
        try:
            run_dir = self.artifact_store.root / dirty_run_id
            capsule = self.artifact_store.verify_capsule(run_dir)
            plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
            binding = capsule["binding"]
            contracts = run_dir / "capsule" / "contracts"
            applier = TrustedDispatcherApplier(
                binding["catalog_slug"],
                primary_profile=binding["primary_profile"],
                logical_profile_id=binding["logical_profile_id"],
                companion_profiles=binding["companion_profiles"],
                dispatcher=contracts / "run-scenario.sh",
                profile_control=contracts / "profile-control.py",
                run_dir=run_dir,
            )
            result = await asyncio.to_thread(
                applier.cleanup,
                CleanupRequest(
                    run_id=dirty_run_id,
                    scenario_id=plan["scenario"]["id"],
                    fencing_token=fencing_token,
                    profile_id=binding["logical_profile_id"],
                    idempotency_key=f"manual-cleanup:{dirty_run_id}:{fencing_token}",
                    requested_at=self.clock.now(),
                ),
            )
            # A successful cleanup must bound the run's effect window: without
            # effect_ended_at the clean-window probe treats the dirty run as
            # overlapping forever and the queue can never restart the scenario.
            effect_ended_at = getattr(result, "effect_ended_at", None)
            if result.succeeded and effect_ended_at is None:
                effect_ended_at = self.clock.now()
            atomic_json(
                self.artifact_store.root / dirty_run_id / "cleanup.json",
                {
                    "schema_version": 1,
                    "source": "manual-dirty-cleanup",
                    "succeeded": result.succeeded,
                    "effect_ended_at": (
                        effect_ended_at.isoformat() if effect_ended_at else None
                    ),
                    "reason": result.reason,
                },
            )
            atomic_json(
                self.artifact_store.root / dirty_run_id / "recovery.json",
                {
                    "schema_version": 1,
                    "source": "manual-dirty-cleanup",
                    "status": "succeeded" if result.succeeded else "failed",
                },
            )
            if result.succeeded:
                self.coordinator.clear_dirty(
                    run_id=dirty_run_id,
                    fencing_token=fencing_token,
                    recovery_verified=True,
                )
                success = True
        except Exception as error:
            self._append_log(f"[ERROR] External DIRTY cleanup failed: {error}")
        finally:
            if not success:
                with suppress(RuntimeError):
                    self.coordinator.finish_cleanup_claim(
                        run_id=dirty_run_id,
                        fencing_token=fencing_token,
                        claimant=claimant,
                    )
        finished = self.clock.now()
        self._current = self._current.model_copy(
            update={
                "status": "succeeded" if success else "failed",
                "finished_at": finished,
                "exit_code": 0 if success else 1,
                "dirty": not success,
            }
        )

    async def _execute(
        self,
        script_path: Path,
        scenario_id: str,
        mode: Literal["run", "cleanup"],
        execution: ExecutionSpec,
        scenario: object,
        lease_run_id: str,
        fencing_token: int,
        manual_dirty_cleanup: bool,
        run_dir: Path | None,
        skip_isolation_checks: bool = False,
    ) -> None:
        assert self._current is not None
        run_id = self._current.run_id
        log_file_path = self.log_path(run_id)

        async with self._lock:
            heartbeat_task = None
            if not manual_dirty_cleanup:
                heartbeat_task = asyncio.create_task(
                    self._heartbeat_loop(lease_run_id, fencing_token)
                )
            with log_file_path.open("w", encoding="utf-8") as log_file:
                controller_session = None
                injection = getattr(scenario, "injection", None)
                is_controller_run = (
                    getattr(scenario, "controller", None) is not None
                    and isinstance(injection, dict)
                    and isinstance(injection.get("profile_id"), str)
                    and mode == "run"
                )
                if is_controller_run:
                    assert run_dir is not None
                    exit_code, controller_session = await self._execute_adaptive(
                        scenario=scenario,
                        run_id=run_id,
                        lease_run_id=lease_run_id,
                        fencing_token=fencing_token,
                        run_dir=run_dir,
                        log_file=log_file,
                        skip_isolation_checks=skip_isolation_checks,
                    )
                else:
                    exit_code = await self._invoke_once(
                        execution, script_path, scenario_id, mode, log_file
                    )
                heartbeat_failed = False
                if heartbeat_task is not None and heartbeat_task.done():
                    heartbeat_error = heartbeat_task.exception()
                    if heartbeat_error is not None:
                        heartbeat_failed = True
                        self._append_log(f"[ERROR] Coordinator heartbeat failed: {heartbeat_error}")
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except asyncio.CancelledError:
                        pass
                    except Exception as heartbeat_error:  # noqa: BLE001 - lease boundary
                        if not heartbeat_failed:
                            heartbeat_failed = True
                            self._append_log(
                                f"[ERROR] Coordinator heartbeat failed: {heartbeat_error}"
                            )

                clean = False
                dirty = False
                if manual_dirty_cleanup:
                    recovered = exit_code == 0 and await self._recovery_verified(scenario)
                    if recovered:
                        self.coordinator.clear_dirty(
                            run_id=lease_run_id,
                            fencing_token=fencing_token,
                            recovery_verified=True,
                        )
                        clean = True
                    else:
                        self.coordinator.finish_cleanup_claim(
                            run_id=lease_run_id,
                            fencing_token=fencing_token,
                            claimant=run_id,
                        )
                        dirty = True
                elif mode == "cleanup":
                    recovered = exit_code == 0 and await self._recovery_verified(scenario)
                    if recovered and not heartbeat_failed:
                        self.coordinator.release(
                            run_id=lease_run_id, fencing_token=fencing_token
                        )
                        clean = True
                    else:
                        self._mark_dirty(lease_run_id, fencing_token, "cleanup_or_recovery_failed")
                        dirty = True
                elif is_controller_run:
                    if (
                        controller_session is not None
                        and controller_session.status == SessionStatus.DIRTY
                    ) or heartbeat_failed:
                        self._mark_dirty(
                            lease_run_id,
                            fencing_token,
                            "adaptive_controller_dirty" if not heartbeat_failed else "heartbeat_failed",
                        )
                        dirty = True
                    else:
                        self.coordinator.release(run_id=lease_run_id, fencing_token=fencing_token)
                        clean = True
                        if (
                            controller_session is not None
                            and controller_session.status == SessionStatus.CLEAN
                        ):
                            try:
                                self._schedule_capture(controller_session, run_dir)
                            except Exception as error:  # capture is required but not a dirty testbed
                                exit_code = -1
                                self._append_log(f"[ERROR] Capture scheduling failed: {error}")
                elif exit_code == 0 and not heartbeat_failed:
                    self.coordinator.release(run_id=lease_run_id, fencing_token=fencing_token)
                    clean = True
                else:
                    dirty = not await self._attempt_failure_cleanup(
                        execution=execution,
                        script_path=script_path,
                        scenario_id=scenario_id,
                        scenario=scenario,
                        lease_run_id=lease_run_id,
                        fencing_token=fencing_token,
                        claimant=run_id,
                        log_file=log_file,
                    )
                    clean = not dirty

            status: Status = "succeeded" if exit_code == 0 and clean else "failed"

        finished_at = self.clock.now()
        duration = (finished_at - self._current.started_at).total_seconds()
        self._current = self._current.model_copy(
            update={
                "status": status,
                "finished_at": finished_at,
                "exit_code": exit_code,
                "dirty": dirty,
            }
        )
        self._history.append(
            HistoryEntry(
                run_id=run_id,
                scenario_id=self._current.scenario_id,
                mode=mode,
                status=status,
                started_at=self._current.started_at,
                finished_at=finished_at,
                duration_sec=duration,
                exit_code=exit_code,
                fencing_token=fencing_token,
                dirty=dirty,
            )
        )

    async def _invoke_once(
        self,
        execution: ExecutionSpec,
        script_path: Path,
        scenario_id: str,
        mode: Literal["run", "cleanup"],
        log_file,
    ) -> int:
        try:
            invocation = build_invocation(execution, script_path, scenario_id, mode)
            proc = await asyncio.create_subprocess_exec(
                *invocation.argv,
                stdin=(asyncio.subprocess.PIPE if invocation.stdin_bytes is not None else None),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=execution_environment(),
            )
            if invocation.stdin_bytes is not None:
                assert proc.stdin is not None
                proc.stdin.write(invocation.stdin_bytes)
                await proc.stdin.drain()
                proc.stdin.close()
            assert proc.stdout is not None
            try:
                await asyncio.wait_for(
                    self._stream_output(proc.stdout, log_file), timeout=execution.timeout_sec
                )
                await proc.wait()
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                self._append_log("[ERROR] Execution timed out and was killed")
                return -1
            return proc.returncode if proc.returncode is not None else -1
        except Exception as error:  # noqa: BLE001 - dispatch boundary
            self._append_log(f"[ERROR] Runner failure: {error}")
            return -1

    async def _execute_adaptive(
        self,
        *,
        scenario: object,
        run_id: str,
        lease_run_id: str,
        fencing_token: int,
        run_dir: Path,
        log_file=None,
        skip_isolation_checks: bool = False,
    ) -> tuple[int, object | None]:
        """Drive and persist a controller session; effects stay dependency-bound."""

        def run_log(line: str) -> None:
            if log_file is not None:
                log_file.write(line + "\n")
                log_file.flush()

        try:
            if self.runtime_factory is not None:
                runtime = self.runtime_factory(
                    scenario=scenario,
                    run_id=run_id,
                    fencing_token=fencing_token,
                    clock=self.clock,
                )
            else:
                runtime = production_runtime(
                    scenario=scenario,
                    run_id=run_id,
                    fencing_token=fencing_token,
                    clock=self.clock,  # type: ignore[arg-type]
                    evidence_path=Path(
                        os.environ.get("BASELINE_EVIDENCE_PATH", "/app/state/baseline-evidence.json")
                    ),
                    observation_path=Path(
                        os.environ.get("OBSERVATION_SNAPSHOT_PATH", "/app/state/observations.json")
                    ),
                    dispatcher=self.dispatcher_path,
                    run_dir=run_dir,
                    skip_isolation_checks=skip_isolation_checks,
                )
            session = await runtime.begin()
            self.artifact_store.persist_session(
                run_dir, session.model_dump(mode="json")
            )
            run_log(
                f"[BEGIN] run={run_id} scenario={getattr(session, 'scenario_id', '?')} "
                f"status={session.status.value} blocked={getattr(session, 'blocked_reasons', [])}"
            )
            while session.status in {SessionStatus.ACTIVE, SessionStatus.RECOVERING}:
                await self.sleeper(session.spec.tick_interval_sec)
                await asyncio.to_thread(
                    self.coordinator.heartbeat,
                    run_id=lease_run_id,
                    fencing_token=fencing_token,
                    now=self.clock.now(),
                    lease_sec=self.lease_sec,
                )
                session = await runtime.tick()
                self.artifact_store.persist_session(
                    run_dir, session.model_dump(mode="json")
                )
                self._record_tick(runtime, session, run_dir, run_log)
            state = getattr(session, "controller_state", None)
            run_log(
                f"[END] status={session.status.value} "
                f"phase={state.phase.value if state else None} "
                f"reason={state.reason if state else None}"
            )
            exit_code = 0 if session.status == SessionStatus.CLEAN else 2
            return exit_code, session
        except Exception as error:  # no untrusted fallback after controller start
            self._append_log(f"[ERROR] Adaptive controller failed closed: {error}")
            run_log(f"[ERROR] Adaptive controller failed closed: {error}")
            if "runtime" in locals():
                session = runtime.session
                if not session.level_changes:
                    session = session.model_copy(
                        update={
                            "status": SessionStatus.BLOCKED,
                            "blocked_reasons": [
                                *session.blocked_reasons,
                                f"runtime_error:{type(error).__name__}",
                            ],
                        }
                    )
                    runtime.session = session
                elif session.cleanup is None and session.controller_state is not None:
                    runtime.session = session.model_copy(
                        update={
                            "controller_state": session.controller_state.model_copy(
                                update={
                                    "phase": ControllerPhase.ABORTED,
                                    "action": ControllerAction.ABORT,
                                    "reason": f"runner_exception:{type(error).__name__}",
                                }
                            ),
                            "terminal_at": self.clock.now(),
                        },
                        deep=True,
                    )
                    await runtime._cleanup_terminal()
                    session = runtime.session
                if session.status not in {SessionStatus.CLEAN, SessionStatus.BLOCKED}:
                    session = session.model_copy(update={"status": SessionStatus.DIRTY})
                    runtime.session = session
                self.artifact_store.persist_session(
                    run_dir, session.model_dump(mode="json")
                )
                return -1, session
            return -1, None

    def _record_tick(self, runtime, session, run_dir: Path, run_log) -> None:
        """Persist the tick diagnostic (ticks.jsonl + run log line); never fatal."""
        tick = getattr(runtime, "last_tick", None)
        state = getattr(session, "controller_state", None)
        try:
            if tick is not None:
                record = {
                    **tick,
                    "phase": state.phase.value if state else None,
                    "action": state.action.value if state else None,
                    "reason": state.reason if state else None,
                    "streaks": dict(state.streaks) if state else {},
                    "status": session.status.value,
                }
                self.artifact_store.append_tick(run_dir, record)
                signals = " ".join(
                    f"{name}={value.get('value')}"
                    + ("" if value.get("usable") else f"!{value.get('quality')}/{value.get('freshness')}")
                    for name, value in record["signals"].items()
                )
                run_log(
                    f"[TICK] {record['at']} +{record['elapsed_sec']}s "
                    f"phase={record['phase']} action={record['action']} "
                    f"reason={record['reason']} streaks={record['streaks']} {signals}"
                )
            elif state is not None:
                run_log(
                    f"[TICK] (no-poll) phase={state.phase.value} "
                    f"action={state.action.value} reason={state.reason}"
                )
        except Exception as error:  # diagnostics must never fail the run
            self._append_log(f"[ERROR] Tick diagnostics failed: {error}")

    def _schedule_capture(self, session, run_dir: Path) -> None:
        if not session.spec.capture.enabled or session.t1 is None or session.t2 is None:
            return
        controller_succeeded = (
            session.controller_state is not None
            and session.controller_state.phase == ControllerPhase.SUCCEEDED
        )
        mode = session.spec.adaptive.mode.value
        if not controller_succeeded:
            # Keep failed attempts in the run diagnostics only. Publishing them
            # beside accepted cases makes them indistinguishable to consumers.
            self.artifact_store.write_result(run_dir, session.trusted_evidence())
            return
        case_token = re.sub(r"[^a-z0-9]+", "-", session.scenario_id.lower()).strip("-")
        case_id = f"case-{case_token}-{session.run_id[-8:].lower()}"
        evidence = None
        if mode == "evaluation":
            if not self.dispatcher_path.is_file() or not self.catalog_path.is_file():
                raise RuntimeError("trusted dispatcher/catalog is unavailable for evaluation")
            evidence = CaptureEvidence(
                approved_profile_id=session.approved_profile_id,
                profile_id=session.profile_id,
                plan_sha256=file_sha256(run_dir / "plan.json"),
                script_sha256=file_sha256(self.dispatcher_path),
                catalog_sha256=file_sha256(self.catalog_path),
                script_path=str(self.dispatcher_path.resolve()),
                catalog_path=str(self.catalog_path.resolve()),
            )
        request = CaptureRequest(
            run_id=session.run_id,
            case_id=case_id,
            scenario_id=session.scenario_id,
            scenario_metadata=load_scenario_metadata(
                self.scenario_metadata_path, session.scenario_id
            ),
            mode=mode,
            t1=session.trusted_evidence()["t1"],
            t2=session.trusted_evidence()["t2"],
            evidence=evidence,
        )
        job = self.capture_scheduler.schedule(request)
        result = job.trusted_result() if mode == "evaluation" else session.trusted_evidence()
        result = {**result, "case_id": case_id}
        self.artifact_store.write_result(run_dir, result)
        self.ensure_capture_worker()

    def ensure_capture_worker(self) -> None:
        if self._capture_task is None or self._capture_task.done():
            self._capture_task = asyncio.create_task(self._capture_loop())

    async def _capture_loop(self) -> None:
        while True:
            pending = [
                job.run_id
                for job in self.capture_scheduler.snapshot().jobs.values()
                if job.status not in {"completed", "failed"}
            ]
            if not pending:
                return
            for run_id in pending:
                try:
                    await asyncio.to_thread(self.capture_scheduler.tick, run_id)
                except Exception as error:  # scheduler persists this job as failed
                    self._append_log(f"[ERROR] Capture job {run_id} failed: {error}")
            await self.sleeper(5)

    async def stop_capture_worker(self) -> None:
        if self._capture_task is None:
            return
        self._capture_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._capture_task
        self._capture_task = None

    def ensure_watchdog_worker(self) -> None:
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def _watchdog_loop(self) -> None:
        while True:
            try:
                await self.watchdog_service.tick()
            except Exception as error:
                self._append_log(f"[ERROR] Watchdog tick failed: {error}")
            await self.sleeper(max(1, self.heartbeat_interval_sec))

    async def stop_watchdog_worker(self) -> None:
        if self._watchdog_task is None:
            return
        self._watchdog_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._watchdog_task
        self._watchdog_task = None

    async def _heartbeat_loop(self, run_id: str, fencing_token: int) -> None:
        while True:
            await self.sleeper(self.heartbeat_interval_sec)
            await asyncio.to_thread(
                self.coordinator.heartbeat,
                run_id=run_id,
                fencing_token=fencing_token,
                now=self.clock.now(),
                lease_sec=self.lease_sec,
            )

    async def _recovery_verified(self, scenario: object) -> bool:
        if self.recovery_validator is None:
            return getattr(scenario, "controller", None) is None
        try:
            result = self.recovery_validator(scenario)
            if inspect.isawaitable(result):
                result = await result
            return result is True
        except Exception as error:  # noqa: BLE001 - validation boundary
            self._append_log(f"[ERROR] Recovery validation failed: {error}")
            return False

    async def _attempt_failure_cleanup(
        self,
        *,
        execution: ExecutionSpec,
        script_path: Path,
        scenario_id: str,
        scenario: object,
        lease_run_id: str,
        fencing_token: int,
        claimant: str,
        log_file,
    ) -> bool:
        claimed = False
        try:
            self.coordinator.claim_cleanup(
                run_id=lease_run_id,
                fencing_token=fencing_token,
                claimant=claimant,
                now=self.clock.now(),
            )
            claimed = True
            self._append_log("[INFO] Attempting fenced cleanup after failed run")
            cleanup_code = await self._invoke_once(
                execution, script_path, scenario_id, "cleanup", log_file
            )
            if cleanup_code == 0 and await self._recovery_verified(scenario):
                self.coordinator.release(
                    run_id=lease_run_id, fencing_token=fencing_token
                )
                return True
            self._mark_dirty(lease_run_id, fencing_token, "automatic_cleanup_or_recovery_failed")
            return False
        except Exception as error:  # noqa: BLE001 - cleanup boundary
            self._append_log(f"[ERROR] Fenced cleanup failed: {error}")
            self._mark_dirty(lease_run_id, fencing_token, "automatic_cleanup_claim_failed")
            return False
        finally:
            if claimed:
                state = self.coordinator.snapshot()
                if state.dirty_run is not None and state.cleanup_claim is not None:
                    with suppress(RuntimeError):
                        self.coordinator.finish_cleanup_claim(
                            run_id=lease_run_id,
                            fencing_token=fencing_token,
                            claimant=claimant,
                        )

    def _mark_dirty(self, run_id: str, fencing_token: int, reason: str) -> None:
        with suppress(RuntimeError):
            self.coordinator.mark_dirty(
                run_id=run_id,
                fencing_token=fencing_token,
                reason=reason,
                now=self.clock.now(),
            )

    @staticmethod
    def _kubernetes_preflight_plan() -> dict:
        configured_path = os.environ.get("KUBECONFIG", _CANONICAL_KUBECONFIG)
        configured_context = os.environ.get("KUBE_CONTEXT", _CANONICAL_KUBE_CONTEXT)
        return {
            "execution": "planned_only",
            "cluster_contact": False,
            "checks": [
                {
                    "id": "canonical-kubeconfig-path",
                    "expected": _CANONICAL_KUBECONFIG,
                    "configured": configured_path,
                    "passed": configured_path == _CANONICAL_KUBECONFIG,
                },
                {
                    "id": "canonical-kube-context",
                    "expected": _CANONICAL_KUBE_CONTEXT,
                    "configured": configured_context,
                    "passed": configured_context == _CANONICAL_KUBE_CONTEXT,
                },
            ],
            "expected_nodes": ["tb-cp", "tb-w1", "tb-w2", "tb-w3"],
        }

    def _coordinator_plan(self) -> dict:
        return {
            "single_active_lease": True,
            "dirty_blocks_new_runs": True,
            "fenced_cleanup_claim": True,
            "state_path": str(self.coordinator.state_path),
        }

    async def _stream_output(
        self, stream: asyncio.StreamReader, log_file
    ) -> None:
        while True:
            line = await stream.readline()
            if not line:
                break
            decoded = line.decode("utf-8", errors="replace").rstrip("\n")
            self._append_log(decoded)
            log_file.write(decoded + "\n")
            log_file.flush()

    def _append_log(self, line: str) -> None:
        self._log_buffer.append(line)


_singleton: Optional[ScenarioRunner] = None


def get_runner() -> ScenarioRunner:
    global _singleton
    if _singleton is None:
        script_dir = Path(os.environ.get("SCRIPT_DIR", "/app/scripts"))
        log_dir = Path(os.environ.get("LOG_DIR", "/app/logs"))
        _singleton = ScenarioRunner(script_dir=script_dir, log_dir=log_dir)
    return _singleton


def reset_runner_for_tests(script_dir: Path, log_dir: Path) -> ScenarioRunner:
    """Tests only: rebind the singleton to given directories."""
    global _singleton
    _singleton = ScenarioRunner(script_dir=script_dir, log_dir=log_dir)
    return _singleton
