import asyncio
import os
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from app.models import HistoryEntry, RunInfo, Status
from app.scenarios import get_scenario

_LOG_TAIL_SIZE = 200
_HISTORY_SIZE = 20
_DEFAULT_TIMEOUT_SEC = 600


class ScenarioRunner:
    """
    Single-active-run runner. Only one scenario (or cleanup) may execute at a time.
    State lives in-process; container restarts lose history but that's acceptable for v1.
    """

    def __init__(self, script_dir: Path, log_dir: Path) -> None:
        self.script_dir = script_dir
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)

        self._lock = asyncio.Lock()
        self._current: Optional[RunInfo] = None
        self._log_buffer: deque[str] = deque(maxlen=_LOG_TAIL_SIZE)
        self._history: deque[HistoryEntry] = deque(maxlen=_HISTORY_SIZE)
        self._task: Optional[asyncio.Task] = None

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

    def get_history(self) -> list[HistoryEntry]:
        return list(reversed(self._history))

    def log_path(self, run_id: str) -> Path:
        return self.log_dir / f"{run_id}.log"

    async def start(
        self,
        scenario_id: str,
        mode: Literal["run", "cleanup"],
    ) -> RunInfo:
        scenario = get_scenario(scenario_id)
        if scenario is None:
            raise ValueError(f"Unknown scenario: {scenario_id}")

        if self._lock.locked() or self.is_busy:
            raise RuntimeError("Another scenario is already running")

        script_path = self.script_dir / scenario.script_filename
        if not script_path.exists():
            raise FileNotFoundError(f"Script not found: {script_path}")

        run_id = f"{scenario_id}-{mode}-{uuid.uuid4().hex[:8]}"
        status: Status = "cleanup_running" if mode == "cleanup" else "running"
        self._current = RunInfo(
            run_id=run_id,
            scenario_id=scenario_id,
            mode=mode,
            status=status,
            started_at=datetime.now(timezone.utc),
        )
        self._log_buffer.clear()

        self._task = asyncio.create_task(
            self._execute(script_path=script_path, mode=mode)
        )
        return self.get_current()  # type: ignore[return-value]

    async def _execute(self, script_path: Path, mode: Literal["run", "cleanup"]) -> None:
        assert self._current is not None
        run_id = self._current.run_id
        log_file_path = self.log_path(run_id)

        args = [str(script_path)]
        if mode == "cleanup":
            args.append("cleanup")

        env = os.environ.copy()
        env.setdefault("KUBECONFIG", "/root/.kube/config")

        async with self._lock:
            try:
                with log_file_path.open("w", encoding="utf-8") as log_file:
                    proc = await asyncio.create_subprocess_exec(
                        "bash",
                        *args,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        env=env,
                    )
                    assert proc.stdout is not None

                    try:
                        await asyncio.wait_for(
                            self._stream_output(proc.stdout, log_file),
                            timeout=_DEFAULT_TIMEOUT_SEC,
                        )
                        await proc.wait()
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
                        self._append_log("[ERROR] Execution timed out and was killed")

                exit_code = proc.returncode if proc.returncode is not None else -1
                status: Status = "succeeded" if exit_code == 0 else "failed"
            except Exception as e:  # noqa: BLE001 - boundary catch
                self._append_log(f"[ERROR] Runner failure: {e}")
                exit_code = -1
                status = "failed"

        finished_at = datetime.now(timezone.utc)
        duration = (finished_at - self._current.started_at).total_seconds()
        self._current = self._current.model_copy(
            update={
                "status": status,
                "finished_at": finished_at,
                "exit_code": exit_code,
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
            )
        )

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
