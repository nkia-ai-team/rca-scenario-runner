"""Production boundaries for the adaptive controller and capture scheduler.

Every side effect is represented by a fixed executable path and an injected
process runner.  Controller YAML can select only approved query/profile IDs;
it can never provide an argv fragment or query body.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from app.adaptive_runtime import (
    ApplyRequest,
    ApplyResult,
    CleanupRequest,
    CleanupResult,
    EligibilityEvidence,
    EligibilityRequest,
)
from app.capture_orchestration import CaptureJob, MODEL_PATH
from app.observations import (
    ApprovedQuery,
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
from app.live_probes import (
    BASELINE_PAID_ORDERS_SQL,
    BLOCKED_SESSION_SQL,
    COMMERCE_OUTBOX_UNPUBLISHED_SQL,
    INDEX_PRESENT_SQL,
    INVENTORY_ZERO_STOCK_SQL,
    PAYMENT_DUPLICATE_SINCE_T1_SQL,
    PG_SLOW_ACTIVE_QUERY_SQL,
    RESTOCK_MOVEMENT_SQL,
    LiveProbeSet,
    TAGGED_SESSION_SQL,
)
from app.adaptive_runtime import AdaptiveRuntime


TRUSTED_RUNS_ROOT = Path("/var/lib/lucida/scenario-runs")
TRUSTED_CONTRACT_ROOT = Path("/opt/lucida/scenario-contracts")
TRUSTED_DISPATCHER = TRUSTED_CONTRACT_ROOT / "run-scenario.sh"
TRUSTED_PROFILE_CONTROL = TRUSTED_CONTRACT_ROOT / "profile-control.py"
TRUSTED_CAPTURE_SCRIPT = Path("/opt/lucida/capture-eval-case.sh")
TRUSTED_CATALOG = TRUSTED_CONTRACT_ROOT / "catalog.json"
TRUSTED_SCENARIO_METADATA = TRUSTED_CONTRACT_ROOT / "registry" / "scenario-metadata.json"

ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]

# Approved database probes whose SQL is fully self-contained: there is no
# parameter to bind through PGOPTIONS, so approval is the whole contract.
#
# 2026-07-30: all five were reaching the dispatch's "not approved" branch, and
# LiveProbes wraps its checks in _safe_bool — so the refusal surfaced as a plain
# False instead of an error. baseline-business-success, which 44 of 44 live
# scenarios require in preflight, could therefore never pass; the first live run
# after the judging-contract repair was blocked on it while commerce was serving
# 192 PAID orders per five minutes against a threshold of 5. The four
# observation probes below were dead the same way, F04-H's unpublished-outbox
# evidence among them.
PARAMETERLESS_DATABASE_PROBES = {
    BASELINE_PAID_ORDERS_SQL: "paid_count",
    COMMERCE_OUTBOX_UNPUBLISHED_SQL: "unpublished_count",
    PG_SLOW_ACTIVE_QUERY_SQL: "slow_active_count",
    INVENTORY_ZERO_STOCK_SQL: "zero_stock_count",
    RESTOCK_MOVEMENT_SQL: "restock_count",
}


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


def compile_trusted_plan(
    scenario_slug: str,
    *,
    dispatcher: Path = TRUSTED_DISPATCHER,
    process_runner: ProcessRunner = subprocess.run,
) -> dict[str, Any]:
    completed = process_runner(
        [str(dispatcher), "--scenario", scenario_slug, "--plan"],
        check=True,
        capture_output=True,
        text=True,
        env=_trusted_environment(),
    )
    document = json.loads(completed.stdout)
    plan = document["normalized_plan"]
    if plan["scenario"]["slug"] != scenario_slug or plan.get("live_allowed") is not True:
        raise RuntimeError("compiled plan does not authorize this live scenario")
    return plan


def atomic_json(path: Path, document: Mapping[str, Any], *, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class RunArtifactStore:
    """Atomic controller evidence under the capture script's fixed trust root."""

    def __init__(self, root: Path = TRUSTED_RUNS_ROOT) -> None:
        self.root = root

    def create(self, run_id: str, plan: Mapping[str, Any]) -> Path:
        run_dir = self.root / run_id
        if run_dir.parent != self.root or not run_id or "/" in run_id:
            raise ValueError("invalid trusted run id")
        run_dir.mkdir(parents=True, exist_ok=False, mode=0o750)
        os.chmod(run_dir, 0o750)
        atomic_json(run_dir / "plan.json", plan)
        for name in ("decisions.json", "timeline.json", "cleanup.json", "recovery.json"):
            atomic_json(run_dir / name, {"schema_version": 1, "events": []})
        return run_dir

    def prepare_capsule(
        self,
        run_id: str,
        *,
        contract_root: Path,
        scenario_slug: str,
        binding: Mapping[str, Any],
        process_runner: ProcessRunner = subprocess.run,
    ) -> tuple[Path, dict[str, Any]]:
        """Atomically publish an immutable, hash-addressed recovery capsule."""
        run_dir = self.root / run_id
        if run_dir.parent != self.root or not run_id or "/" in run_id:
            raise ValueError("invalid trusted run id")
        if not contract_root.is_dir() or contract_root.is_symlink():
            raise RuntimeError("trusted contract root must be a real directory")
        for source in contract_root.rglob("*"):
            if source.is_symlink() or not (source.is_dir() or source.is_file()):
                raise RuntimeError("trusted contract root contains a link or special file")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o750)
        staging = Path(tempfile.mkdtemp(prefix=f".{run_id}.capsule.", dir=self.root))
        try:
            contracts = staging / "capsule" / "contracts"
            shutil.copytree(contract_root, contracts, symlinks=False)
            hashes: dict[str, str] = {}
            for path in sorted(contracts.rglob("*")):
                if path.is_symlink() or not (path.is_dir() or path.is_file()):
                    raise RuntimeError("capsule contains a link or special file")
                if path.is_dir():
                    os.chmod(path, 0o750)
                    continue
                relative = path.relative_to(contracts).as_posix()
                executable = bool(path.stat().st_mode & 0o111)
                os.chmod(path, 0o750 if executable else 0o640)
                hashes[relative] = file_sha256(path)
            manifest = {"schema_version": 1, "files": hashes}
            atomic_json(staging / "capsule" / "hashes.json", manifest)
            plan = compile_trusted_plan(
                scenario_slug,
                dispatcher=contracts / "run-scenario.sh",
                process_runner=process_runner,
            )
            atomic_json(staging / "plan.json", plan)
            atomic_json(
                staging / "capsule.json",
                {
                    "schema_version": 1,
                    "scenario_slug": scenario_slug,
                    "binding": dict(binding),
                    "hash_manifest_sha256": file_sha256(staging / "capsule" / "hashes.json"),
                    "plan_sha256": file_sha256(staging / "plan.json"),
                },
            )
            for name in ("decisions.json", "timeline.json", "cleanup.json", "recovery.json"):
                atomic_json(staging / name, {"schema_version": 1, "events": []})
            atomic_json(staging / "mutations.json", {"schema_version": 1, "operations": []})
            os.chmod(staging, 0o750)
            os.replace(staging, run_dir)
            root_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
            return run_dir, plan
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def repair_capsule_contracts(
        self, run_dir: Path, contract_root: Path, *, reason: str, now: datetime
    ) -> dict[str, Any]:
        """Re-materialize a dirty run's contract tree from the live trusted root.

        The capsule freezes two different things: `plan.json` says *what* was
        applied, and `capsule/contracts` says *how* to undo it. Freezing the first
        is the whole point — cleanup must target exactly what injection created.
        Freezing the second permanently is what deadlocks us: if the executor that
        cleans up is itself defective, the run can never reach a verified-clean
        state, and because DIRTY is global that wedges every scenario, not just
        the failed one. 2026-07-30: F21-P died on an unquoted `&`, and the same
        defect sat inside its capsule, so its own cleanup could not run.

        Repair therefore replaces the mechanism while holding the target fixed:

        - `plan.json` is not touched and must still match `capsule.json`'s
          `plan_sha256`. If what we are undoing changed, this is not a repair.
        - The previous hash manifest is preserved in `capsule-repair.json` with
          the reason and timestamp, so the swap is recorded rather than hidden.
        - Repair is explicit, never automatic on cleanup failure. A capsule that
          silently re-arms itself from the live tree is not frozen at all.
        - It clears nothing by itself. Cleanup and recovery still have to pass
          before `clear_dirty`, so "no residue" stays machine-verified — repair
          only lets the machine get to the question.
        """
        capsule = self.verify_capsule(run_dir)
        if not contract_root.is_dir() or contract_root.is_symlink():
            raise RuntimeError("trusted contract root must be a real directory")
        contracts = run_dir / "capsule" / "contracts"
        previous_manifest_sha256 = capsule["hash_manifest_sha256"]

        staging = Path(tempfile.mkdtemp(prefix=f".{run_dir.name}.repair.", dir=self.root))
        try:
            rebuilt = staging / "contracts"
            shutil.copytree(contract_root, rebuilt, symlinks=False)
            hashes: dict[str, str] = {}
            for path in sorted(rebuilt.rglob("*")):
                if path.is_symlink() or not (path.is_dir() or path.is_file()):
                    raise RuntimeError("repaired capsule contains a link or special file")
                if path.is_dir():
                    os.chmod(path, 0o750)
                    continue
                relative = path.relative_to(rebuilt).as_posix()
                executable = bool(path.stat().st_mode & 0o111)
                os.chmod(path, 0o750 if executable else 0o640)
                hashes[relative] = file_sha256(path)
            atomic_json(staging / "hashes.json", {"schema_version": 1, "files": hashes})
            manifest_sha256 = file_sha256(staging / "hashes.json")

            shutil.rmtree(contracts)
            os.replace(rebuilt, contracts)
            os.replace(staging / "hashes.json", run_dir / "capsule" / "hashes.json")
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        record_path = run_dir / "capsule-repair.json"
        record: dict[str, Any] = {"schema_version": 1, "repairs": []}
        if record_path.is_file():
            record = json.loads(record_path.read_text(encoding="utf-8"))
        repair = {
            "reason": reason,
            "repaired_at": now.isoformat(),
            "contract_root": str(contract_root.resolve()),
            "previous_hash_manifest_sha256": previous_manifest_sha256,
            "hash_manifest_sha256": manifest_sha256,
        }
        record["repairs"].append(repair)
        atomic_json(record_path, record)
        atomic_json(
            run_dir / "capsule.json",
            {**capsule, "hash_manifest_sha256": manifest_sha256},
        )
        # The plan is deliberately re-verified after the rewrite: a repair that
        # moved the target would be a different run wearing this run's id.
        self.verify_capsule(run_dir)
        return repair

    def verify_capsule(self, run_dir: Path) -> dict[str, Any]:
        if run_dir.parent != self.root or run_dir.is_symlink():
            raise RuntimeError("capsule escaped the trusted runs root")
        capsule = json.loads((run_dir / "capsule.json").read_text(encoding="utf-8"))
        hashes_path = run_dir / "capsule" / "hashes.json"
        if file_sha256(hashes_path) != capsule["hash_manifest_sha256"]:
            raise RuntimeError("capsule hash manifest was modified")
        if file_sha256(run_dir / "plan.json") != capsule["plan_sha256"]:
            raise RuntimeError("capsule normalized plan was modified")
        manifest = json.loads(hashes_path.read_text(encoding="utf-8"))
        contracts = run_dir / "capsule" / "contracts"
        actual: dict[str, str] = {}
        for path in sorted(contracts.rglob("*")):
            if path.is_symlink() or not (path.is_dir() or path.is_file()):
                raise RuntimeError("capsule contains a link or special file")
            if path.is_dir() and (path.stat().st_mode & 0o777) != 0o750:
                raise RuntimeError("capsule directory mode is not trusted")
            if path.is_file():
                mode = path.stat().st_mode & 0o777
                if mode not in {0o640, 0o750}:
                    raise RuntimeError("capsule file mode is not trusted")
                actual[path.relative_to(contracts).as_posix()] = file_sha256(path)
        if actual != manifest["files"]:
            raise RuntimeError("capsule contract hash mismatch")
        plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
        controller = plan.get("controller", {})
        plan_binding = controller.get("binding", {})
        runtime = controller.get("runtime", {})
        if capsule.get("scenario_slug") != plan["scenario"]["slug"]:
            raise RuntimeError("capsule scenario binding was modified")
        if plan_binding:
            expected_binding = {
                "catalog_slug": plan["scenario"]["slug"],
                "primary_profile": plan_binding.get("primary_ref"),
                "logical_profile_id": (
                    plan_binding.get("approved_profile_id")
                    if runtime.get("mode") == "evaluation"
                    else plan_binding.get("primary_ref")
                ),
                "companion_profiles": plan_binding.get("companion_refs", []),
            }
            if capsule.get("binding") != expected_binding:
                raise RuntimeError("capsule profile binding was modified")
        return capsule

    def bind_lease(self, run_dir: Path, *, run_id: str, fencing_token: int) -> None:
        self.verify_capsule(run_dir)
        atomic_json(
            run_dir / "lease.json",
            {"run_id": run_id, "fencing_token": fencing_token},
        )

    def persist_session(self, run_dir: Path, session: Mapping[str, Any]) -> None:
        atomic_json(run_dir / "state.json", session)
        state = session.get("controller_state") or {}
        changes = session.get("level_changes") or []
        atomic_json(run_dir / "decisions.json", {"schema_version": 1, "controller_state": state})
        atomic_json(run_dir / "timeline.json", {"schema_version": 1, "level_changes": changes})
        atomic_json(run_dir / "cleanup.json", {"schema_version": 1, "cleanup": session.get("cleanup")})
        atomic_json(run_dir / "recovery.json", {"schema_version": 1, "recovery": session.get("recovery")})

    def write_result(self, run_dir: Path, result: Mapping[str, Any]) -> Path:
        path = run_dir / "result.json"
        atomic_json(path, result)
        return path

    def append_tick(self, run_dir: Path, record: Mapping[str, Any]) -> None:
        if run_dir.parent != self.root or run_dir.is_symlink():
            raise RuntimeError("tick record escaped the trusted runs root")
        with (run_dir / "ticks.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")


class ArtifactEligibilityProbe:
    """Read only, fail-closed baseline evidence supplied by an approved producer."""

    def __init__(self, evidence_path: Path, *, clock: SystemClock | None = None) -> None:
        self.evidence_path = evidence_path
        self.clock = clock or SystemClock()

    def inspect(self, request: EligibilityRequest) -> EligibilityEvidence:
        raw = json.loads(self.evidence_path.read_text(encoding="utf-8"))
        evidence = EligibilityEvidence.model_validate(raw)
        if evidence.checked_at > self.clock.now():
            raise RuntimeError("baseline evidence is from the future")
        expected_start = request.requested_at - timedelta(seconds=request.clean_window_sec)
        if evidence.clean_window_start > expected_start or evidence.clean_window_end < request.requested_at:
            raise RuntimeError("baseline evidence does not cover the requested clean window")
        return evidence


class ArtifactObservationReader:
    """Read approved query results from a fixed producer-owned JSON snapshot."""

    def __init__(self, snapshot_path: Path) -> None:
        self.snapshot_path = snapshot_path

    def __call__(self, query: ApprovedQuery) -> Mapping[str, Any]:
        document = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        values = document.get("queries", {})
        value = values.get(query.query_id)
        if not isinstance(value, Mapping):
            raise RuntimeError(f"approved observation is unavailable: {query.query_id}")
        return value


class AsyncLiveProbeSet:
    """Keep read-only network/process probes off the coordinator event loop."""

    def __init__(self, probes: LiveProbeSet) -> None:
        self.probes = probes

    async def inspect(self, request):
        return await asyncio.to_thread(self.probes.inspect, request)

    async def observe(self, query):
        return await asyncio.to_thread(self.probes.observe, query)


class AsyncProfileApplier:
    """Keep trusted profile preflight/apply/cleanup off the heartbeat loop."""

    def __init__(self, applier: "TrustedDispatcherApplier") -> None:
        self.applier = applier

    async def apply(self, request):
        return await asyncio.to_thread(self.applier.apply, request)

    async def cleanup(self, request):
        return await asyncio.to_thread(self.applier.cleanup, request)

    async def transition_cleanup(self, request):
        return await asyncio.to_thread(self.applier.transition_cleanup, request)


def production_runtime(
    *,
    scenario: Any,
    run_id: str,
    fencing_token: int,
    clock: SystemClock,
    evidence_path: Path,
    observation_path: Path,
    dispatcher: Path = TRUSTED_DISPATCHER,
    run_dir: Path | None = None,
    process_runner: ProcessRunner = subprocess.run,
    live_probes: LiveProbeSet | None = None,
    artifact_fallback: bool = False,
    skip_isolation_checks: bool = False,
) -> AdaptiveRuntime:
    if run_dir is not None:
        store = RunArtifactStore(run_dir.parent)
        store.verify_capsule(run_dir)
        dispatcher = run_dir / "capsule" / "contracts" / "run-scenario.sh"
    injection = scenario.injection or {}
    profile_id = injection.get("profile_id")
    if not isinstance(profile_id, str) or not profile_id:
        raise RuntimeError("controller scenario requires injection.profile_id")
    approved = injection.get("approved_profile_id")
    if approved is not None and not isinstance(approved, str):
        raise RuntimeError("approved_profile_id must be a string")
    catalog_slug = injection.get("catalog_slug")
    if not isinstance(catalog_slug, str) or not catalog_slug:
        raise RuntimeError("controller scenario requires injection.catalog_slug")
    companions = injection.get("companion_profile_ids", [])
    if not isinstance(companions, list) or not all(
        isinstance(item, str) and item for item in companions
    ):
        raise RuntimeError("companion_profile_ids must be a list of profile ids")
    if live_probes is None and not artifact_fallback:
        live_probes = _configured_live_probes(
            run_id=run_id,
            scenario_id=scenario.id,
            clock=clock,
            process_runner=process_runner,
        )
    asynchronous_probes = AsyncLiveProbeSet(live_probes) if live_probes is not None else None
    reader = (
        ArtifactObservationReader(observation_path)
        if asynchronous_probes is None
        else asynchronous_probes.observe
    )
    adapters = {
        "loadgen_summary": LoadgenSummaryAdapter(reader, clock=clock.now),
        "http_probe": HttpProbeAdapter(reader, clock=clock.now),
        "prometheus": PrometheusAdapter(reader, clock=clock.now),
        "kubernetes": KubernetesAdapter(reader, clock=clock.now),
        "database": DatabaseAdapter(reader, clock=clock.now),
        "host_probe": HostProbeAdapter(reader, clock=clock.now),
        "business_probe": BusinessProbeAdapter(reader, clock=clock.now),
        "capture_status": CaptureStatusAdapter(reader, clock=clock.now),
        "clickhouse": ClickHouseAdapter(reader, clock=clock.now),
    }
    evaluation = scenario.controller.adaptive.mode.value == "evaluation"
    session_profile_id = approved if evaluation else profile_id
    if not isinstance(session_profile_id, str) or not session_profile_id:
        raise RuntimeError("evaluation requires an approved fixed profile id")
    return AdaptiveRuntime.create(
        run_id=run_id,
        scenario_id=scenario.id,
        fencing_token=fencing_token,
        profile_id=session_profile_id,
        approved_profile_id=approved,
        spec=scenario.controller,
        clock=clock,
        skip_isolation_checks=skip_isolation_checks,
        eligibility_probe=(
            ArtifactEligibilityProbe(evidence_path, clock=clock)
            if asynchronous_probes is None
            else asynchronous_probes
        ),
        poller=ObservationPoller(ApprovedQueryRegistry.from_path(), adapters),
        applier=AsyncProfileApplier(
            TrustedDispatcherApplier(
                catalog_slug,
                primary_profile=profile_id,
                logical_profile_id=session_profile_id,
                companion_profiles=companions,
                dispatcher=dispatcher,
                profile_control=(
                    run_dir / "capsule" / "contracts" / "profile-control.py"
                    if run_dir is not None
                    else TRUSTED_PROFILE_CONTROL
                ),
                run_dir=run_dir,
                process_runner=process_runner,
                clock=clock,
            )
        ),
    )


def _configured_live_probes(
    *, run_id: str, scenario_id: str, clock: SystemClock, process_runner: ProcessRunner
) -> LiveProbeSet:
    def http_client(method, url, *, body=None, headers=None, timeout):
        request = urllib.request.Request(url, data=body, headers=dict(headers or {}), method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed URLs
            payload = response.read()
            try:
                decoded = json.loads(payload) if payload else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                decoded = {}
            return {
                "status": response.status,
                "observed_at": clock.now().isoformat(),
                "json": decoded,
            }

    def database_client(sql, parameters, *, credentials):
        if sql == TAGGED_SESSION_SQL and len(parameters) == 1:
            pgoptions = f"-c lucida.scenario_tag={parameters[0]}"
            result_field = "tagged_count"
        elif sql == BLOCKED_SESSION_SQL and parameters in (
            ("inventory_schema.inventory",), ("payment_schema.payments",)
        ):
            pgoptions = f"-c lucida.lock_relation={parameters[0]}"
            result_field = "blocked_count"
        elif sql == INDEX_PRESENT_SQL and parameters == (
            "product_schema", "products", "idx_products_name"
        ):
            pgoptions = (
                "-c lucida.index_schema=product_schema "
                "-c lucida.index_table=products "
                "-c lucida.index_name=idx_products_name"
            )
            result_field = "index_present"
        elif sql == PAYMENT_DUPLICATE_SINCE_T1_SQL and len(parameters) == 1:
            try:
                datetime.fromisoformat(parameters[0].replace("Z", "+00:00"))
            except ValueError as error:
                raise RuntimeError("payment duplicate probe t1 is invalid") from error
            pgoptions = f"-c lucida.payment_t1={parameters[0]}"
            result_field = "duplicate_count"
        elif sql in PARAMETERLESS_DATABASE_PROBES and not parameters:
            pgoptions = ""
            result_field = PARAMETERLESS_DATABASE_PROBES[sql]
        else:
            raise RuntimeError("database probe query is not approved")
        environment = _trusted_environment()
        environment.update(
            {
                "PGHOST": os.environ.get("COMMERCE_DB_HOST", "192.168.122.77"),
                "PGPORT": os.environ.get("COMMERCE_DB_PORT", "30432"),
                "PGDATABASE": os.environ.get("COMMERCE_DB_NAME", "commerce"),
                "PGUSER": str(credentials.get("user", "commerce")),
                "PGPASSWORD": str(credentials.get("password", "")),
                "PGOPTIONS": pgoptions,
            }
        )
        completed = process_runner(
            [
                "psql", "-At", "-c", sql,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            timeout=10,
        )
        raw = completed.stdout.strip()
        if result_field == "index_present":
            if raw not in {"0", "1"}:
                raise RuntimeError("database index probe returned an invalid value")
            value: int | bool = raw == "1"
        else:
            value = int(raw)
        return {result_field: value, "observed_at": clock.now().isoformat()}

    return LiveProbeSet(
        process_runner=process_runner,
        http_client=http_client,
        database_client=database_client,
        database_credentials={
            "user": os.environ.get("COMMERCE_DB_USER", "commerce"),
            "password": os.environ.get("COMMERCE_DB_PASSWORD", ""),
        },
        run_id=run_id,
        scenario_id=scenario_id,
        clock=clock.now,
    )


class TrustedDispatcherApplier:
    """Invoke only the mounted dispatcher, with its digest confirmation contract."""

    def __init__(
        self,
        scenario_slug: str,
        *,
        primary_profile: str,
        logical_profile_id: str | None = None,
        companion_profiles: list[str] | None = None,
        dispatcher: Path = TRUSTED_DISPATCHER,
        profile_control: Path = TRUSTED_PROFILE_CONTROL,
        process_runner: ProcessRunner = subprocess.run,
        clock: SystemClock | None = None,
        run_dir: Path | None = None,
    ) -> None:
        self.scenario_slug = scenario_slug
        self.primary_profile = primary_profile
        self.logical_profile_id = logical_profile_id or primary_profile
        self.companion_profiles = companion_profiles or []
        self.dispatcher = dispatcher
        self.profile_control = profile_control
        self.process_runner = process_runner
        self.clock = clock or SystemClock()
        self.run_dir = run_dir
        self.artifact_store = RunArtifactStore(run_dir.parent) if run_dir is not None else None
        self._applied: dict[str, ApplyResult] = {}
        self._cleaned: dict[str, CleanupResult] = {}

    def _plan(self) -> dict[str, Any]:
        if self.run_dir is not None:
            assert self.artifact_store is not None
            self.artifact_store.verify_capsule(self.run_dir)
        plan = compile_trusted_plan(
            self.scenario_slug,
            dispatcher=self.dispatcher,
            process_runner=self.process_runner,
        )
        if self.run_dir is not None:
            stored = json.loads((self.run_dir / "plan.json").read_text(encoding="utf-8"))
            if plan != stored:
                raise RuntimeError("capsule compiled plan differs from its normalized plan")
        return plan

    def apply(self, request: ApplyRequest) -> ApplyResult:
        existing = self._applied.get(request.idempotency_key)
        if existing is not None:
            return existing
        plan = self._plan()
        profiles = [
            item for item in plan["profile_instances"]
            if item["profile_id"] == self.primary_profile
        ]
        if request.profile_id != self.logical_profile_id:
            raise RuntimeError("adaptive request does not target the trusted logical profile")
        if len(profiles) != 1:
            raise RuntimeError("primary profile is absent from the compiled plan")
        approved_levels = profiles[0].get("approved_levels")
        if approved_levels is None:
            approved_levels = [
                {"level_index": 0, "level_id": request.level_id, "parameters": profiles[0].get("parameters")}
            ]
        approved_level = (
            approved_levels[request.level_index]
            if 0 <= request.level_index < len(approved_levels)
            else None
        )
        if approved_level is not None and approved_level.get("level_id") != request.level_id:
            approved_level = None
        if approved_level is None or approved_level.get("parameters") != request.parameters:
            raise RuntimeError("adaptive level is not an exact compiled profile instance")
        digest = plan["plan_digest"]
        confirmation = f"LIVE:{plan['scenario']['id']}:{digest}"
        if not self.profile_control.is_file():
            raise RuntimeError("trusted per-profile control API is unavailable")
        plan_profiles = {item["profile_id"]: item for item in plan["profile_instances"]}
        unknown = set(self.companion_profiles) - set(plan_profiles)
        if unknown:
            raise RuntimeError(f"companion profiles are absent from compiled plan: {sorted(unknown)}")
        responses: list[dict[str, Any]] = []
        for profile_id in [*self.companion_profiles, self.primary_profile]:
            operation_key = (
                f"companion:{request.run_id}:{request.fencing_token}:"
                f"{request.level_index}:{profile_id}"
                if profile_id != self.primary_profile
                else request.idempotency_key
            )
            argv = [
                str(self.profile_control), "--scenario", self.scenario_slug,
                "--profile", profile_id, "--action", "apply",
                "--run-id", request.run_id, "--fencing-token", str(request.fencing_token),
                "--idempotency-key", operation_key,
            ]
            if profile_id == self.primary_profile:
                argv.extend(
                    [
                        "--level-index", str(request.level_index),
                        "--level-id", request.level_id,
                        "--parameters-json",
                        json.dumps(request.parameters, sort_keys=True, separators=(",", ":")),
                    ]
                )
            argv.extend(["--plan-digest", digest, "--confirm", confirmation])
            self._journal(operation_key, profile_id, "apply", "intent")
            completed = self.process_runner(
                argv,
                check=True,
                capture_output=True,
                text=True,
                env=_trusted_environment(),
            )
            self._journal(operation_key, profile_id, "apply", "complete")
            responses.append(json.loads(completed.stdout))
        if not responses:
            raise RuntimeError("profile transaction applied no profiles")
        applied_at = min(
            datetime.fromisoformat(item["applied_at"].replace("Z", "+00:00"))
            for item in responses
        )
        result = ApplyResult(
            run_id=request.run_id,
            fencing_token=request.fencing_token,
            idempotency_key=request.idempotency_key,
            applied_at=applied_at,
        )
        self._applied[request.idempotency_key] = result
        return result

    def cleanup(self, request: CleanupRequest) -> CleanupResult:
        existing = self._cleaned.get(request.idempotency_key)
        if existing is not None:
            return existing
        plan = self._plan()
        digest = plan["plan_digest"]
        confirmation = f"LIVE:{plan['scenario']['id']}:{digest}"
        try:
            if not self.profile_control.is_file():
                raise RuntimeError("trusted per-profile control API is unavailable")
            response = None
            intended = self._intended_profiles()
            cleanup_order = [self.primary_profile, *reversed(self.companion_profiles)]
            for profile_id in [item for item in cleanup_order if not intended or item in intended]:
                operation_key = f"{request.idempotency_key}:{profile_id}"
                self._journal(operation_key, profile_id, "cleanup", "intent")
                completed = self.process_runner(
                    [
                        str(self.profile_control), "--scenario", self.scenario_slug,
                        "--profile", profile_id, "--action", "cleanup",
                        "--run-id", request.run_id, "--fencing-token", str(request.fencing_token),
                        "--idempotency-key", operation_key,
                        "--plan-digest", digest, "--confirm", confirmation,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                    env=_trusted_environment(),
                )
                current = json.loads(completed.stdout)
                self._journal(operation_key, profile_id, "cleanup", "complete")
                if current["succeeded"] is not True:
                    response = current
                    break
                response = current
            assert response is not None
            effect_ended_at = (
                datetime.fromisoformat(response["effect_ended_at"].replace("Z", "+00:00"))
                if response["succeeded"] is True
                else None
            )
            result = CleanupResult(
                run_id=request.run_id,
                fencing_token=request.fencing_token,
                idempotency_key=request.idempotency_key,
                succeeded=response["succeeded"] is True,
                effect_ended_at=effect_ended_at if response["succeeded"] is True else None,
                reason=response.get("reason"),
            )
        except Exception as error:  # fail closed at the dispatcher boundary
            result = CleanupResult(
                run_id=request.run_id,
                fencing_token=request.fencing_token,
                idempotency_key=request.idempotency_key,
                succeeded=False,
                reason=f"trusted_dispatcher:{type(error).__name__}",
            )
        self._cleaned[request.idempotency_key] = result
        return result

    def _journal(self, key: str, profile_id: str, action: str, phase: str) -> None:
        if self.run_dir is None:
            return
        path = self.run_dir / "mutations.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        operations = document.setdefault("operations", [])
        record = next((item for item in operations if item["idempotency_key"] == key), None)
        if record is None:
            record = {
                "idempotency_key": key,
                "profile_id": profile_id,
                "action": action,
                "intent_at": self.clock.now().isoformat(),
                "complete_at": None,
            }
            operations.append(record)
        if phase == "complete":
            record["complete_at"] = self.clock.now().isoformat()
        atomic_json(path, document)

    def _intended_profiles(self) -> set[str]:
        if self.run_dir is None:
            return set()
        document = json.loads((self.run_dir / "mutations.json").read_text(encoding="utf-8"))
        return {
            item["profile_id"]
            for item in document.get("operations", [])
            if item.get("action") == "apply"
        }

    def transition_cleanup(self, request: CleanupRequest) -> CleanupResult:
        """End the whole composite transaction before the next level starts."""
        return self.cleanup(request)


class ProductionCaptureInvoker:
    """Checkpoint the model first, then run stores using that checkpoint."""

    def __init__(
        self,
        *,
        runs_root: Path = TRUSTED_RUNS_ROOT,
        capture_script: Path = TRUSTED_CAPTURE_SCRIPT,
        output_root: Path = Path("/data/eval-cases"),
        model_source: Path = Path(MODEL_PATH),
        model_container: str = "lucida-ai-observer",
        model_ssh_target: str | None = None,
        model_ssh_key: Path = Path("/root/.ssh/tb_key"),
        process_runner: ProcessRunner = subprocess.run,
    ) -> None:
        self.runs_root = runs_root
        self.capture_script = capture_script
        self.output_root = output_root
        self.model_source = model_source
        self.model_container = model_container
        self.model_ssh_target = model_ssh_target
        self.model_ssh_key = model_ssh_key
        self.process_runner = process_runner

    def snapshot_model(self, job: CaptureJob, *, idempotency_key: str) -> None:
        checkpoint = self.runs_root / job.run_id / "model.json"
        if checkpoint.exists():
            return
        checkpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        temporary = checkpoint.with_suffix(".tmp")
        if self.model_source.is_file():
            shutil.copyfile(self.model_source, temporary)
        elif self.model_ssh_target:
            completed = self.process_runner(
                [
                    "ssh", "-i", str(self.model_ssh_key),
                    "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                    "-o", "ConnectTimeout=10", self.model_ssh_target,
                    "docker", "exec", self.model_container, "cat", MODEL_PATH,
                ],
                check=True,
                capture_output=True,
                text=True,
                env=_trusted_environment(),
                timeout=30,
            )
            temporary.write_text(completed.stdout, encoding="utf-8")
        else:
            self.process_runner(
                ["docker", "cp", f"{self.model_container}:{MODEL_PATH}", str(temporary)],
                check=True,
                capture_output=True,
                text=True,
                env=_trusted_environment(),
            )
        json.loads(temporary.read_text(encoding="utf-8"))
        os.chmod(temporary, 0o640)
        os.replace(temporary, checkpoint)
        atomic_json(
            checkpoint.parent / "model-checkpoint.json",
            {"idempotency_key": idempotency_key, "path": str(checkpoint), "sha256": file_sha256(checkpoint)},
        )

    def dump_stores(self, job: CaptureJob, *, idempotency_key: str) -> None:
        checkpoint = self.runs_root / job.run_id / "model.json"
        if not checkpoint.is_file():
            raise RuntimeError("model checkpoint is missing")
        if job.scenario_metadata is None:
            raise RuntimeError("AI-authored scenario metadata is missing")
        metadata = job.scenario_metadata
        args = [
            str(self.capture_script), "--case-id", job.case_id,
            "--scenario-id", job.scenario_id,
            "--scenario-title", metadata.title,
            "--scenario-description", metadata.description,
            "--scenario-cause", metadata.cause,
            "--scenario-injection-summary", metadata.injection_summary,
            "--scenario-user-impact", metadata.user_impact,
            "--scenario-distinguishing-evidence", metadata.distinguishing_evidence,
            "--scenario-metadata-sha256", metadata.sha256(),
            "--t1", job.t1, "--t2", job.t2,
            "--case-label", job.mode, "--output-root", str(self.output_root),
        ]
        if job.mode == "evaluation":
            args.extend(["--run-result", str(self.runs_root / job.run_id / "result.json")])
        # Capture contract v2 (spec §2.1): the queue drops the 1st-layer preflight
        # verdict and the day's normal-segment path next to the run; forward them
        # so meta.json gets preflight/segments/rebase and assembled/ is built.
        preflight_file = self.runs_root / job.run_id / "preflight.json"
        if preflight_file.is_file():
            args.extend(["--preflight-json", str(preflight_file)])
        normal_marker = self.runs_root / job.run_id / "normal-segment.path"
        if normal_marker.is_file():
            normal_dir = normal_marker.read_text(encoding="utf-8").strip()
            if normal_dir:
                args.extend(["--normal-segment", normal_dir])
        # Capture contract v3 (spec §2.2): in cycle mode the queue drops the
        # continuous-cycle phase log next to the run; forward it so meta.json
        # gets phases[] and the capture window is [cycle_start, t2+30m].
        phases_file = self.runs_root / job.run_id / "phases.json"
        if phases_file.is_file():
            args.extend(["--phases-json", str(phases_file)])
        # Cycle topology bundle (2026-07-24 EventCluster contract): the queue drops
        # a marker with the per-cycle bundle dir of periodic topology snapshots;
        # forward it so the case includes the raw topology graph + service tree.
        topology_marker = self.runs_root / job.run_id / "topology-bundle.path"
        if topology_marker.is_file():
            topology_dir = topology_marker.read_text(encoding="utf-8").strip()
            if topology_dir:
                args.extend(["--topology-bundle", topology_dir])
        environment = _trusted_environment()
        environment["MODEL_SOURCE"] = str(checkpoint)
        try:
            self.process_runner(args, check=True, capture_output=True, text=True, env=environment)
        except subprocess.CalledProcessError as error:
            log_path = self.runs_root / job.run_id / "capture-error.log"
            log_path.write_text(
                f"stdout:\n{error.stdout or ''}\nstderr:\n{error.stderr or ''}\n",
                encoding="utf-8",
            )
            detail = (error.stderr or error.stdout or str(error)).strip().splitlines()
            raise RuntimeError(
                f"capture command failed: {detail[-1] if detail else type(error).__name__}"
            ) from error
        atomic_json(
            self.runs_root / job.run_id / "capture-complete.json",
            {
                "idempotency_key": idempotency_key,
                "case_id": job.case_id,
                "observed_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        self._close_open_incidents(job)

    def _close_open_incidents(self, job: CaptureJob) -> None:
        """Clean-slate policy (2026-07-21): close open incidents at capture end.

        Runs after the capture window is fully exported so the incident's
        in-window lifecycle is untouched; before the next scenario so the
        judge cannot merge its anomalies into a stale incident. Fail-open —
        never fails the capture; the outcome (or error) lands next to the run.
        """
        from app.incident_close import close_open_incidents

        record = self.runs_root / job.run_id / "incidents-closed.json"
        try:
            closed = close_open_incidents()
            atomic_json(
                record,
                {
                    "closed": closed,
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as error:
            atomic_json(
                record,
                {
                    "error": f"{type(error).__name__}: {error}",
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                },
            )


def _trusted_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["KUBECONFIG"] = "/root/tb-kubeconfig"
    for name in (
        "RUNNER_SCRIPT", "SCENARIO_CATALOG", "SCENARIO_MANIFEST_DIR",
        "SCENARIO_REGISTRY_DIR", "SCENARIO_DISPATCHER_PATH", "SCENARIO_STATE_DIR",
    ):
        environment.pop(name, None)
    return environment
