"""Unit tests for the cycle topology snapshot collector."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from app.topology_collector import TopologyCollector


class FakeClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 16, 8, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class FakeFetch:
    """Map the graph/tree URLs to canned raw bytes; toggle failures per part."""

    def __init__(self, graph: bytes, tree: bytes) -> None:
        self.graph = graph
        self.tree = tree
        self.fail_graph = False
        self.graph_status = 200
        self.calls: list[str] = []

    def __call__(self, url: str) -> tuple[int, bytes]:
        self.calls.append(url)
        if "topology/graph" in url:
            if self.fail_graph:
                raise RuntimeError("connection refused")
            return self.graph_status, self.graph
        return 200, self.tree


def _collector(tmp_path: Path, fetch, clock, **kw) -> TopologyCollector:
    return TopologyCollector(
        bundle_dir=tmp_path / "bundle",
        fetch=fetch,
        clock=clock,
        run_id=kw.pop("run_id", None),
        interval_sec=kw.pop("interval_sec", 30),
    )


def test_collect_once_saves_raw_pair_and_updates_manifest(tmp_path: Path) -> None:
    clock = FakeClock()
    fetch = FakeFetch(
        graph=b'{"nodes":[],"edges":[],"truncated":false}', tree=b'{"tree":[1,2]}'
    )
    collector = _collector(tmp_path, fetch, clock, run_id="run-abc")

    collector.collect_once()
    clock.advance(30)
    collector.collect_once()

    manifest = json.loads((tmp_path / "bundle" / "manifest.json").read_text())
    assert manifest["schema_version"] == "1"
    assert manifest["case_id"] is None
    assert manifest["run_id"] == "run-abc"
    assert manifest["capture_interval_seconds"] == 30
    assert len(manifest["snapshots"]) == 2
    assert manifest["capture_failures"] == []

    snap = manifest["snapshots"][0]
    assert snap["captured_at"] == "2026-07-16T08:00:00Z"
    graph = snap["graph"][0]
    assert graph["part"] == 1 and graph["part_count"] == 1 and graph["anchors"] == []
    assert graph["truncated"] is False
    assert graph["http_status"] == 200
    graph_file = tmp_path / "bundle" / graph["path"]
    assert graph_file.read_bytes() == fetch.graph  # raw, unmodified
    assert graph["sha256"] == hashlib.sha256(fetch.graph).hexdigest()

    tree = snap["service_tree"]
    assert (tmp_path / "bundle" / tree["path"]).read_bytes() == fetch.tree
    assert tree["sha256"] == hashlib.sha256(fetch.tree).hexdigest()


def test_failed_fetch_is_recorded_and_collection_continues(tmp_path: Path) -> None:
    clock = FakeClock()
    fetch = FakeFetch(graph=b'{"truncated":false}', tree=b'{"tree":[]}')
    collector = _collector(tmp_path, fetch, clock)

    fetch.fail_graph = True
    collector.collect_once()  # graph transport error, tree ok

    manifest = json.loads((tmp_path / "bundle" / "manifest.json").read_text())
    assert len(manifest["capture_failures"]) == 1
    failure = manifest["capture_failures"][0]
    assert "topology/graph" in failure["request_url"]
    assert failure["http_status"] is None
    assert "connection refused" in failure["error"]
    # Blank recorded: the tick still appears with an empty graph and a tree.
    snap = manifest["snapshots"][0]
    assert snap["graph"] == []
    assert snap["service_tree"] is not None

    # The collector keeps going: the next tick recovers.
    fetch.fail_graph = False
    clock.advance(30)
    collector.collect_once()
    manifest = json.loads((tmp_path / "bundle" / "manifest.json").read_text())
    assert len(manifest["snapshots"]) == 2
    assert len(manifest["snapshots"][1]["graph"]) == 1
    # No graph file was written for the failed tick, one for the recovered tick.
    graph_files = sorted((tmp_path / "bundle" / "graph").glob("*.json"))
    assert len(graph_files) == 1


def test_non_2xx_status_is_a_recorded_failure(tmp_path: Path) -> None:
    clock = FakeClock()
    fetch = FakeFetch(graph=b'{"truncated":true}', tree=b'{}')
    fetch.graph_status = 503
    collector = _collector(tmp_path, fetch, clock)

    collector.collect_once()

    manifest = json.loads((tmp_path / "bundle" / "manifest.json").read_text())
    assert manifest["snapshots"][0]["graph"] == []
    failure = manifest["capture_failures"][0]
    assert failure["http_status"] == 503
    assert "topology/graph" in failure["request_url"]
    # No graph file persisted for a non-2xx response.
    assert not (tmp_path / "bundle" / "graph").exists()
