import { useMemo } from "react";

import { fmtStamp, fmtTime, relTime } from "../lib/format";
import { presentationFor, TONE } from "../lib/tones";
import type {
  DisplayStatus,
  HistoryEntry,
  ScenarioView,
} from "../types";
import { Icon } from "./Icon";
import { Pill } from "./Pill";
import { StatusBadge } from "./StatusBadge";

interface Props {
  history: HistoryEntry[];
  scenarios: ScenarioView[];
  open: boolean;
  setOpen: (open: boolean) => void;
}

function toDisplay(status: HistoryEntry["status"]): DisplayStatus {
  if (status === "cleanup_running") return "running";
  return status;
}

export function HistoryTable({ history, scenarios, open, setOpen }: Props) {
  const lookup = useMemo(() => {
    const m = new Map<string, ScenarioView>();
    for (const s of scenarios) m.set(s.id, s);
    return m;
  }, [scenarios]);

  const succ = history.filter((h) => h.status === "succeeded").length;
  const fail = history.filter((h) => h.status === "failed").length;
  const shown = history.slice(0, 10);

  return (
    <div className="card overflow-hidden">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between px-5 py-3.5 hover:bg-[var(--bg-2)] transition-colors"
      >
        <div className="flex items-center gap-3">
          <Icon
            name="chev"
            className={`w-4 h-4 text-[var(--ink-3)] transition-transform ${
              open ? "rotate-180" : "-rotate-90"
            }`}
          />
          <span className="text-[14px] font-semibold text-[var(--ink)] tracking-[-0.01em]">
            Execution History
          </span>
          <span className="text-[11.5px] text-[var(--ink-3)]">
            실행 이력 · 총 {history.length} 건
          </span>
        </div>
        <div className="flex items-center gap-2">
          {history.length > 0 && (
            <>
              <Pill tone="emerald">{succ} 성공</Pill>
              {fail > 0 && <Pill tone="rose">{fail} 실패</Pill>}
            </>
          )}
        </div>
      </button>

      {open && (
        <div className="border-t hair overflow-x-auto fade-in">
          {history.length === 0 ? (
            <div className="p-10 text-center">
              <div className="text-[13px] text-[var(--ink-2)]">
                아직 실행 이력이 없습니다
              </div>
              <div className="mono text-[11.5px] text-[var(--ink-3)] mt-1">
                시나리오를 실행하면 여기에 기록됩니다
              </div>
            </div>
          ) : (
            <>
              <table className="w-full text-[13px]">
                <thead>
                  <tr className="text-left mono text-[10.5px] uppercase tracking-wider text-[var(--ink-3)] border-b hair">
                    <th className="pl-5 pr-3 py-2.5 font-normal w-14">#</th>
                    <th className="px-3 py-2.5 font-normal">Scenario</th>
                    <th className="px-3 py-2.5 font-normal w-52">시작 시각</th>
                    <th className="px-3 py-2.5 font-normal w-24">소요</th>
                    <th className="px-3 py-2.5 font-normal w-28">결과</th>
                    <th className="pl-3 pr-5 py-2.5 font-normal w-28 text-right">
                      액션
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {shown.map((h, i) => {
                    const scn = lookup.get(h.scenario_id);
                    const pres = presentationFor(h.scenario_id);
                    const toneDot = TONE[pres.tone].dot;
                    const startedAt = new Date(h.started_at);
                    const duration = h.duration_sec ?? 0;
                    return (
                      <tr
                        key={h.run_id}
                        className="border-b hair last:border-b-0 hover:bg-[var(--bg-2)]"
                      >
                        <td className="pl-5 pr-3 py-3 mono text-[var(--ink-3)]">
                          {String(history.length - i).padStart(3, "0")}
                        </td>
                        <td className="px-3 py-3">
                          <div className="flex items-center gap-2">
                            <span
                              className={`w-1.5 h-1.5 rounded-full ${toneDot}`}
                            />
                            <span className="text-[var(--ink)] font-medium">
                              {scn?.name ?? h.scenario_id}
                            </span>
                            <span className="mono text-[10.5px] text-[var(--ink-3)]">
                              S{pres.num}
                            </span>
                            {h.mode === "cleanup" && (
                              <span className="mono text-[10px] text-[var(--ink-3)] uppercase tracking-wider">
                                cleanup
                              </span>
                            )}
                          </div>
                        </td>
                        <td className="px-3 py-3">
                          <div className="mono text-[12px] text-[var(--ink-2)]">
                            {fmtStamp(startedAt)}
                          </div>
                          <div className="text-[10.5px] text-[var(--ink-3)] mt-0.5">
                            {relTime(startedAt)}
                          </div>
                        </td>
                        <td className="px-3 py-3 mono text-[var(--ink)] tabular-nums">
                          {fmtTime(duration)}
                        </td>
                        <td className="px-3 py-3">
                          <StatusBadge status={toDisplay(h.status)} />
                        </td>
                        <td className="pl-3 pr-5 py-3 text-right">
                          <button className="inline-flex items-center gap-1 text-[11.5px] text-[var(--ink-2)] hover:text-violet-600">
                            <Icon name="eye" className="w-3.5 h-3.5" />
                            로그 보기
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {history.length > 10 && (
                <div className="border-t hair px-5 py-3 flex items-center justify-between bg-[var(--bg-2)]">
                  <span className="mono text-[11px] text-[var(--ink-3)]">
                    전체 {history.length} 건 중 최근 10 건 표시
                  </span>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
