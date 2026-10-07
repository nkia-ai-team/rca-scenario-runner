"""Periodic topology-graph + service-tree snapshot collector for cycle capture.

Contract (2026-07-24, EventCluster eval consumer): during each v3 cycle the
runner periodically snapshots the auto-discovered topology graph and the
operator service tree, bundling the RAW responses into the case. Every attempt
— success or failure — is recorded with a sha256 over the raw bytes so
consumers can trust provenance; blanks (all-failed ticks) are recorded too.

Fail-open by design: collector errors never stall the live queue. A failed
fetch lands in ``manifest.capture_failures`` and the loop keeps going.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from pathlib import Path

# Endpoints (spec §4 topology snapshot rules; global-graph mode per 2026-07-24
# live measurement — 171 nodes/172 edges, truncated=False, so no anchor split).
TOPOLOGY_GRAPH_PATH = "/api/v1/topology/graph"
SERVICE_TREE_PATH = "/api/v1/asset-tree/service/unified"
GRAPH_QUERY = {
    "range": "24h",
    "depth": "1",
    "edgeKinds": (
        "host_socket,apm_call,apm_db,apm_host,network_link,access_host,"
        "k8s_service_pod,k8s_workload_pod,k8s_pod_node,ebpf_process_service,"
        "ebpf_socket,cloud_lb_target,cloud_dependency"
    ),
    "includeSelfMonitor": "true",
}
DEFAULT_INTERVAL_SEC = 30
DEFAULT_QUERY_URL = "http://192.168.230.119:18080"
_LOGIN_TIMEOUT_SEC = 10
_FETCH_TIMEOUT_SEC = 20

# (url) -> (http_status, raw_response_bytes). Raising signals a transport error.
Fetch = Callable[[str], "tuple[int, bytes]"]


def _query_string() -> str:
    return "&".join(f"{key}={value}" for key, value in GRAPH_QUERY.items())


class TopologyCollector:
    """One collector per cycle; ``collect_once`` is a single deterministic tick."""

    def __init__(
        self,
        *,
        bundle_dir: Path,
        fetch: Fetch,
        clock,
        run_id: str | None = None,
        interval_sec: int = DEFAULT_INTERVAL_SEC,
        query_url: str = DEFAULT_QUERY_URL,
        sleeper: "Callable[[float], Awaitable[None]]" = asyncio.sleep,
    ) -> None:
        self.bundle_dir = Path(bundle_dir)
        self._fetch = fetch
        self._clock = clock
        self.run_id = run_id
        self.interval_sec = interval_sec
        self._sleeper = sleeper
        base = query_url.rstrip("/")
        self.graph_url = f"{base}{TOPOLOGY_GRAPH_PATH}?{_query_string()}"
        self.tree_url = f"{base}{SERVICE_TREE_PATH}"
        self._snapshots: list[dict] = []
        self._failures: list[dict] = []
        self._task: asyncio.Task | None = None
        self._stopped = False

    def set_run_id(self, run_id: str) -> None:
        """Bind the injection run (known only once buffer ends). Persisted on the
        next manifest write so the capture step can fill case_id later."""
        self.run_id = run_id

    def collect_once(self) -> None:
        """Fetch the graph + service-tree pair once, save raw bytes, and rewrite
        the manifest. Records every attempt; never raises for a fetch failure."""
        now = self._clock.now()
        captured_at = _format_utc(now)
        stamp = captured_at.replace(":", "-")
        snapshot: dict = {"snapshot_id": captured_at, "captured_at": captured_at}

        graph = self._capture_part(
            self.graph_url,
            rel_path=f"graph/{stamp}-part-001.json",
            captured_at=captured_at,
            graph_part=True,
        )
        snapshot["graph"] = [graph] if graph is not None else []

        tree = self._capture_part(
            self.tree_url,
            rel_path=f"service-tree/{stamp}.json",
            captured_at=captured_at,
            graph_part=False,
        )
        snapshot["service_tree"] = tree

        self._snapshots.append(snapshot)
        self._write_manifest()

    def _capture_part(
        self, url: str, *, rel_path: str, captured_at: str, graph_part: bool
    ) -> dict | None:
        try:
            status, body = self._fetch(url)
        except Exception as error:  # transport failure — record the blank
            self._failures.append(
                {
                    "attempted_at": captured_at,
                    "request_url": url,
                    "http_status": None,
                    "error": str(error),
                }
            )
            return None
        if not (200 <= int(status) < 300):
            self._failures.append(
                {
                    "attempted_at": captured_at,
                    "request_url": url,
                    "http_status": int(status),
                    "error": f"non-2xx response: {status}",
                }
            )
            return None
        # Success: write the RAW bytes verbatim (no re-serialisation) and sha the
        # exact bytes so consumers can verify the untouched original.
        target = self.bundle_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(target, body)
        descriptor: dict = {
            "path": rel_path,
            "request_url": url,
            "http_status": int(status),
            "sha256": hashlib.sha256(body).hexdigest(),
        }
        if graph_part:
            descriptor.update({"part": 1, "part_count": 1, "anchors": []})
            truncated = _truncated_flag(body)
            if truncated is not None:
                descriptor["truncated"] = truncated
        return descriptor

    def _write_manifest(self) -> None:
        document = {
            "schema_version": "1",
            "case_id": None,  # capture step fills this from the case it promotes
            "run_id": self.run_id,
            "capture_interval_seconds": self.interval_sec,
            "snapshots": self._snapshots,
            "capture_failures": self._failures,
        }
        manifest = self.bundle_dir / "manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(
            manifest,
            (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )

    async def run(self) -> None:
        while not self._stopped:
            try:
                self.collect_once()
            except Exception:
                # collect_once already records fetch failures; this only guards
                # against unexpected local errors so the loop cannot die.
                pass
            await self._sleeper(self.interval_sec)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run())

    def stop(self) -> None:
        """Signal the loop to end and cancel the task. Sync so cycle ticks (which
        are not async) can stop the collector without awaiting."""
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            self._task = None


def _truncated_flag(body: bytes) -> bool | None:
    try:
        document = json.loads(body)
    except Exception:
        return None
    value = document.get("truncated") if isinstance(document, dict) else None
    return bool(value) if isinstance(value, bool) else None


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("collector clock must be timezone-aware")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(raw, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(raw)


def build_query_fetch(
    *,
    query_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> Fetch:
    """Production ``fetch``: a cookie session against the query API (same login
    as incident_close). Logs in lazily and re-logins once on a 401."""
    base = (query_url or os.environ.get("LUCIDA_QUERY_URL") or DEFAULT_QUERY_URL).rstrip("/")
    user = username or os.environ.get("LUCIDA_LOGIN_USER")
    secret = password or os.environ.get("LUCIDA_LOGIN_PASSWORD")
    if not user or not secret:
        raise RuntimeError("LUCIDA_LOGIN_USER/LUCIDA_LOGIN_PASSWORD are not configured")
    session: dict[str, object] = {"opener": None}

    def _login():
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )
        request = urllib.request.Request(
            f"{base}/api/v1/login",
            data=json.dumps({"username": user, "password": secret}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener.open(request, timeout=_LOGIN_TIMEOUT_SEC) as response:
            if response.status != 200:
                raise RuntimeError(f"lucida login failed: HTTP {response.status}")
        session["opener"] = opener
        return opener

    def _get(opener, url):
        with opener.open(
            urllib.request.Request(url, method="GET"), timeout=_FETCH_TIMEOUT_SEC
        ) as response:
            return int(response.status), response.read()

    def fetch(url: str) -> tuple[int, bytes]:
        opener = session["opener"] or _login()
        try:
            return _get(opener, url)
        except urllib.error.HTTPError as error:
            if error.code == 401:
                return _get(_login(), url)
            return int(error.code), error.read()

    return fetch
