import type { ReactNode } from "react";

import { fmtStamp, fmtTime } from "../lib/format";
import type { DisplayStatus } from "../types";
import type { ExecutionState } from "../hooks/useRunner";
import { Icon } from "./Icon";
import { LogViewer } from "./LogViewer";
import { Pill } from "./Pill";

interface BigStatusMeta {
  bg: string;
  icon: ReactNode;
  label: string;
  sub: string;
}

const STATUS_META: Record<DisplayStatus, BigStatusMeta> = {
  idle: {
    bg: "bg-slate-50 text-slate-700 ring-slate-200",
    icon: <Icon name="dots" className="w-5 h-5" />,
    label: "대기",
    sub: "시나리오를 선택해 실행하세요",
  },
  running: {
    bg: "bg-violet-50 text-violet-800 ring-violet-200",
    icon: <Icon name="spinner" className="w-5 h-5 spin" />,
    label: "실행 중",
    sub: "fault 주입 · telemetry 스트리밍",
  },
  succeeded: {
    bg: "bg-emerald-50 text-emerald-800 ring-emerald-200",
    icon: <Icon name="check" className="w-5 h-5" />,
    label: "성공",
    sub: "스크립트가 exit 0 으로 종료되었습니다",
  },
  failed: {
    bg: "bg-rose-50 text-rose-800 ring-rose-200",
    icon: <Icon name="x" className="w-5 h-5" />,
    label: "실패",
    sub: "스크립트가 non-zero exit 코드로 종료되었습니다",
  },
};

function BigStatus({ status }: { status: DisplayStatus }) {
  const m = STATUS_META[status];
  return (
    <div className={`flex items-center gap-3 rounded-xl ring-1 px-3.5 py-2.5 ${m.bg}`}>
      <div className="w-9 h-9 rounded-lg bg-white/70 ring-1 ring-white flex items-center justify-center">
        {m.icon}
      </div>
      <div>
        <div className="text-[13px] font-semibold tracking-[-0.005em]">{m.label}</div>
        <div className="text-[11.5px] opacity-80">{m.sub}</div>
      </div>
    </div>
  );
}

interface Props {
  exec: ExecutionState | null;
  elapsed: number;
  onCopy: () => Promise<boolean>;
  onDownload: () => void | Promise<void>;
}

export function ExecutionPanel({ exec, elapsed, onCopy, onDownload }: Props) {
  const scn = exec?.scenario ?? null;
  const duration = scn?.estimated_duration_sec ?? 0;
  const status: DisplayStatus = exec?.status ?? "idle";
  const isTerminal = status === "succeeded" || status === "failed";
  const progress = isTerminal
    ? 1
    : duration > 0
      ? Math.min(1, elapsed / duration)
      : 0;

  return (
    <div className="card flex flex-col overflow-hidden h-full relative">
      <div className="p-5 border-b hair relative">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <div className="mono text-[10.5px] uppercase tracking-[0.2em] text-[var(--ink-3)]">
                Current Execution
              </div>
              <span className="text-[var(--ink-3)] text-[10.5px]">·</span>
              <div className="text-[11px] text-[var(--ink-3)]">실시간 실행 패널</div>
            </div>
            <h2 className="mt-1.5 text-[20px] font-semibold text-[var(--ink)] truncate tracking-[-0.015em]">
              {scn ? scn.name : "시나리오를 선택해 주세요"}
            </h2>
            <div className="mt-1.5 flex items-center gap-2 flex-wrap">
              {scn && exec ? (
                <>
                  <Pill tone={scn.tone}>{scn.tag}</Pill>
                  <span className="mono text-[11px] text-[var(--ink-3)]">
                    S{scn.num}
                  </span>
                  <span className="tick-dot" />
                  <span className="mono text-[11px] text-[var(--ink-3)]">
                    {fmtStamp(exec.startedAt)} 시작
                  </span>
                  {exec.mode === "cleanup" && (
                    <Pill tone="violet">cleanup</Pill>
                  )}
                </>
              ) : (
                <span className="text-[11.5px] text-[var(--ink-3)]">
                  좌측 카드에서 실행 버튼을 눌러 주세요
                </span>
              )}
            </div>
          </div>
          <div className="text-right shrink-0">
            <div className="mono text-[10px] uppercase tracking-[0.2em] text-[var(--ink-3)]">
              Elapsed
            </div>
            <div className="mono text-[28px] font-semibold text-[var(--ink)] tabular-nums leading-none mt-1">
              {fmtTime(elapsed)}
            </div>
            {scn && (
              <div className="mono text-[11px] text-[var(--ink-3)] mt-1">
                / {fmtTime(duration)}
              </div>
            )}
          </div>
        </div>
      </div>

      <div className="p-5 border-b hair space-y-4">
        <div className="flex items-center justify-between gap-3">
          <BigStatus status={status} />
          {(status === "succeeded" || status === "failed") &&
            exec?.exitCode !== null &&
            exec?.exitCode !== undefined && (
              <div
                className={`mono text-[11px] rounded-lg ring-1 px-2.5 py-1.5 fade-in ${
                  status === "succeeded"
                    ? "bg-emerald-50 text-emerald-700 ring-emerald-200"
                    : "bg-rose-50 text-rose-700 ring-rose-200"
                }`}
              >
                exit <span className="font-semibold">{exec.exitCode}</span>
              </div>
            )}
        </div>

        <div>
          <div className="flex items-center justify-between mb-1.5">
            <span className="mono text-[10.5px] uppercase tracking-wider text-[var(--ink-3)]">
              진행률
            </span>
            <span className="mono text-[11px] text-[var(--ink-2)] tabular-nums">
              {Math.round(progress * 100)}%
            </span>
          </div>
          <div className="h-1.5 bg-[var(--bg-2)] rounded-full overflow-hidden">
            <div
              className={`h-full transition-all duration-500 ${
                status === "failed"
                  ? "bg-rose-500"
                  : status === "succeeded"
                    ? "bg-emerald-500"
                    : status === "running"
                      ? "shimmer"
                      : "bg-slate-300"
              }`}
              style={{ width: `${progress * 100}%` }}
            />
          </div>
        </div>
      </div>

      <div className="p-4 flex-1 flex flex-col min-h-0 bg-[var(--bg-2)] relative">
        <LogViewer
          lines={exec?.lines ?? []}
          onCopy={onCopy}
          onDownload={onDownload}
        />
      </div>
    </div>
  );
}
