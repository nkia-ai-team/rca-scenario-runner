import { useCallback, useEffect, useRef, useState } from "react";

import { api, ApiError } from "../api/client";
import { parseLogTail } from "../lib/parseLog";
import type {
  DisplayStatus,
  HistoryEntry,
  LogLine,
  RunInfo,
  ScenarioView,
  Status,
} from "../types";

const POLL_INTERVAL_MS = 2000;

export interface ExecutionState {
  scenario: ScenarioView;
  runId: string | null;
  status: DisplayStatus;
  mode: "run" | "cleanup";
  startedAt: Date;
  finishedAt: Date | null;
  exitCode: number | null;
  lines: LogLine[];
}

export interface RunnerApi {
  exec: ExecutionState | null;
  statuses: Record<string, DisplayStatus>;
  history: HistoryEntry[];
  anyRunning: boolean;
  error: string | null;
  run: (scn: ScenarioView) => Promise<void>;
  cleanup: (scn: ScenarioView) => Promise<void>;
  downloadLog: () => Promise<void>;
  copyLog: () => Promise<void>;
}

function toDisplay(status: Status): DisplayStatus {
  if (status === "cleanup_running") return "running";
  return status;
}

function parseIsoSafe(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function useRunner(scenarios: ScenarioView[]): RunnerApi {
  const [statuses, setStatuses] = useState<Record<string, DisplayStatus>>({});
  const [exec, setExec] = useState<ExecutionState | null>(null);
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [error, setError] = useState<string | null>(null);

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const lineCountRef = useRef(0);

  useEffect(() => {
    const next: Record<string, DisplayStatus> = {};
    for (const s of scenarios) next[s.id] = statuses[s.id] ?? "idle";
    if (scenarios.length && Object.keys(next).length !== Object.keys(statuses).length) {
      setStatuses(next);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scenarios]);

  const refreshHistory = useCallback(async () => {
    try {
      const h = await api.history();
      setHistory(h);
    } catch (e) {
      console.warn("history fetch failed", e);
    }
  }, []);

  useEffect(() => {
    refreshHistory();
  }, [refreshHistory]);

  const stopPoll = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const applyRunInfo = useCallback(
    (scn: ScenarioView, info: RunInfo) => {
      const lines = parseLogTail(info.log_tail);
      lineCountRef.current = lines.length;
      const startedAt =
        parseIsoSafe(info.started_at) ?? new Date();
      const finishedAt = parseIsoSafe(info.finished_at);
      const display = toDisplay(info.status);

      setExec({
        scenario: scn,
        runId: info.run_id,
        status: display,
        mode: info.mode,
        startedAt,
        finishedAt,
        exitCode: info.exit_code,
        lines,
      });
      setStatuses((prev) => ({ ...prev, [scn.id]: display }));
    },
    [],
  );

  const startPolling = useCallback(
    (scn: ScenarioView) => {
      stopPoll();
      pollRef.current = setInterval(async () => {
        try {
          const info = await api.status(scn.id);
          applyRunInfo(scn, info);
          if (info.status !== "running" && info.status !== "cleanup_running") {
            stopPoll();
            refreshHistory();
          }
        } catch (e) {
          if (e instanceof ApiError && e.status === 404) {
            // no active run yet — keep trying briefly
            return;
          }
          console.warn("status poll failed", e);
        }
      }, POLL_INTERVAL_MS);
    },
    [applyRunInfo, refreshHistory, stopPoll],
  );

  const run = useCallback(
    async (scn: ScenarioView) => {
      setError(null);
      try {
        lineCountRef.current = 0;
        const info = await api.run(scn.id);
        applyRunInfo(scn, info);
        startPolling(scn);
      } catch (e) {
        if (e instanceof ApiError) {
          setError(
            e.status === 409
              ? "다른 시나리오가 실행 중입니다"
              : (e.detail ?? e.message),
          );
        } else {
          setError("알 수 없는 오류");
        }
      }
    },
    [applyRunInfo, startPolling],
  );

  const cleanup = useCallback(
    async (scn: ScenarioView) => {
      setError(null);
      try {
        const info = await api.cleanup(scn.id);
        applyRunInfo(scn, info);
        startPolling(scn);
      } catch (e) {
        if (e instanceof ApiError) {
          setError(
            e.status === 409
              ? "다른 시나리오가 실행 중입니다"
              : (e.detail ?? e.message),
          );
        } else {
          setError("알 수 없는 오류");
        }
      }
    },
    [applyRunInfo, startPolling],
  );

  useEffect(() => () => stopPoll(), [stopPoll]);

  const linesToText = useCallback(() => {
    if (!exec) return "";
    return exec.lines
      .map(
        (l) =>
          `${l.t.padEnd(8)}  ${l.lvl.toUpperCase().padEnd(5)} ${l.svc.padEnd(
            16,
          )} ${l.msg}`,
      )
      .join("\n");
  }, [exec]);

  const copyLog = useCallback(async () => {
    if (!exec) return;
    try {
      await navigator.clipboard?.writeText(linesToText());
    } catch (e) {
      console.warn("clipboard failed", e);
    }
  }, [exec, linesToText]);

  const downloadLog = useCallback(async () => {
    if (!exec || !exec.runId) return;
    try {
      const text = await api.fullLog(exec.scenario.id, exec.runId);
      const blob = new Blob([text], { type: "text/plain" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${exec.scenario.id}-${exec.runId}.log`;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      // fallback to in-memory
      const blob = new Blob([linesToText()], { type: "text/plain" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${exec.scenario.id}.log`;
      a.click();
      URL.revokeObjectURL(url);
    }
  }, [exec, linesToText]);

  const anyRunning = Object.values(statuses).some((s) => s === "running");

  return {
    exec,
    statuses,
    history,
    anyRunning,
    error,
    run,
    cleanup,
    downloadLog,
    copyLog,
  };
}
