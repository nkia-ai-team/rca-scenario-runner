import { Fragment, useState } from "react";

import { TONE } from "../lib/tones";
import type { DisplayStatus, ScenarioView } from "../types";
import { Icon } from "./Icon";
import { Pill } from "./Pill";
import { StatusBadge } from "./StatusBadge";

interface Props {
  scn: ScenarioView;
  status: DisplayStatus;
  runDisabled: boolean;
  isSelected: boolean;
  onRun: (scn: ScenarioView) => void;
  onCleanup: (scn: ScenarioView) => void;
}

export function ScenarioCard({
  scn,
  status,
  runDisabled,
  isSelected,
  onRun,
  onCleanup,
}: Props) {
  const [open, setOpen] = useState(false);
  const tone = TONE[scn.tone];
  const durationMin = Math.max(1, Math.round(scn.estimated_duration_sec / 60));

  return (
    <div
      className={`card relative overflow-hidden transition-all ${
        isSelected
          ? "ring-2 ring-violet-300 ring-offset-2 ring-offset-[var(--bg)]"
          : "hover:shadow-md"
      }`}
    >
      <div
        className="absolute top-0 left-0 right-0 h-px"
        style={{
          background:
            "linear-gradient(90deg, transparent, var(--hair-2), transparent)",
        }}
      />

      <div className="p-5">
        <div className="flex items-start gap-4">
          <div className="shrink-0 w-12 flex flex-col items-center">
            <div className="num-serif text-[44px] leading-none text-[var(--ink)]">
              {scn.num}
            </div>
            <div className="mt-1 mono text-[9.5px] uppercase tracking-[0.15em] text-[var(--ink-3)]">
              S{scn.num}
            </div>
          </div>

          <div className="flex-1 min-w-0">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="flex items-center gap-2 flex-wrap">
                  <h3 className="text-[15.5px] font-semibold text-[var(--ink)] leading-snug tracking-[-0.01em]">
                    {scn.name}
                  </h3>
                  <Pill tone={scn.tone}>{scn.tag}</Pill>
                </div>
                <p
                  className="text-[13px] text-[var(--ink-2)] mt-1.5 leading-relaxed"
                  style={{ textWrap: "pretty" } as React.CSSProperties}
                >
                  {scn.description}
                </p>
              </div>
              <StatusBadge status={status} />
            </div>

            <div className="mt-3 flex items-center gap-3 text-[11.5px] text-[var(--ink-3)]">
              <span className="mono">≈ {durationMin}분</span>
              <span className="tick-dot" />
              <span className="mono">{scn.expected_alarms.length} alarms</span>
              <span className="tick-dot" />
              <span className="mono">{scn.propagationHops.length} hops</span>
            </div>

            <button
              onClick={() => setOpen((o) => !o)}
              className="mt-3 w-full flex items-center justify-between text-[12px] text-[var(--ink-2)] hover:text-[var(--ink)] pt-3 border-t hair"
            >
              <span className="flex items-center gap-1.5">
                <Icon
                  name="chev"
                  className={`w-3.5 h-3.5 transition-transform ${
                    open ? "rotate-180" : ""
                  }`}
                />
                상세 보기
              </span>
              <span className="mono text-[10.5px] uppercase tracking-wider text-[var(--ink-3)]">
                {open ? "접기" : "펼치기"}
              </span>
            </button>

            {open && (
              <div className="mt-3 grid grid-cols-[88px_1fr] gap-x-3 gap-y-2.5 text-[12.5px] slide-up">
                <div className="mono text-[10.5px] uppercase tracking-wider text-[var(--ink-3)] pt-0.5">
                  원인
                </div>
                <div className="text-[var(--ink)]">{scn.cause}</div>

                <div className="mono text-[10.5px] uppercase tracking-wider text-[var(--ink-3)] pt-0.5">
                  전파
                </div>
                <div className="flex items-center gap-1.5 flex-wrap">
                  {scn.propagationHops.map((p, i) => (
                    <Fragment key={i}>
                      <span className="mono text-[11px] text-[var(--ink)] bg-[var(--bg-2)] ring-1 ring-[var(--hair)] rounded-md px-1.5 py-0.5">
                        {p}
                      </span>
                      {i < scn.propagationHops.length - 1 && (
                        <Icon
                          name="arr"
                          className="w-3 h-3 text-[var(--ink-3)]"
                        />
                      )}
                    </Fragment>
                  ))}
                </div>

                <div className="mono text-[10.5px] uppercase tracking-wider text-[var(--ink-3)] pt-0.5">
                  예상 알람
                </div>
                <div>
                  <ul className="space-y-1">
                    {scn.expected_alarms.map((a, i) => (
                      <li
                        key={i}
                        className="flex items-start gap-2 text-[var(--ink)]"
                      >
                        <span
                          className={`mt-[7px] w-1 h-1 rounded-full ${tone.dot}`}
                        />
                        <span className="mono text-[11.5px]">{a}</span>
                      </li>
                    ))}
                  </ul>
                </div>

                {scn.warnings.length > 0 && (
                  <>
                    <div className="mono text-[10.5px] uppercase tracking-wider text-amber-700 pt-0.5">
                      주의사항
                    </div>
                    <div className="rounded-md bg-amber-50 ring-1 ring-amber-200 px-2.5 py-2">
                      <ul className="space-y-1.5">
                        {scn.warnings.map((w, i) => (
                          <li
                            key={i}
                            className="flex items-start gap-2 text-amber-900"
                          >
                            <span className="mt-[7px] w-1 h-1 rounded-full bg-amber-600 shrink-0" />
                            <span className="text-[12px] leading-relaxed">{w}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  </>
                )}
              </div>
            )}

            <div className="mt-4 flex items-center gap-2">
              <div
                className="tip"
                data-tip={
                  runDisabled ? "다른 시나리오가 실행 중입니다" : undefined
                }
              >
                <button
                  onClick={() => !runDisabled && onRun(scn)}
                  disabled={runDisabled}
                  className={`inline-flex items-center gap-1.5 px-3.5 py-2 rounded-lg text-[12.5px] font-medium transition ${
                    runDisabled
                      ? "bg-slate-100 text-slate-400 cursor-not-allowed"
                      : "bg-[var(--ink)] text-white hover:bg-black shadow-sm hover:shadow"
                  }`}
                >
                  <Icon name="play" className="w-3.5 h-3.5" />
                  실행
                </button>
              </div>
              <button
                onClick={() => onCleanup(scn)}
                className="inline-flex items-center gap-1.5 px-3 py-2 rounded-lg text-[12.5px] font-medium text-[var(--ink-2)] ring-1 ring-[var(--hair)] bg-white hover:ring-[var(--hair-2)] hover:text-[var(--ink)]"
              >
                <Icon name="broom" className="w-3.5 h-3.5" />
                Cleanup
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
