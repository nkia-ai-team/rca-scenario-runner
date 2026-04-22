import { fmtTime } from "../lib/format";
import type { HistoryEntry } from "../types";

interface Props {
  anyRunning: boolean;
  scenarioCount: number;
  history: HistoryEntry[];
}

function today(): string {
  return new Date().toLocaleDateString("ko-KR", {
    year: "numeric",
    month: "long",
    day: "numeric",
    weekday: "long",
  });
}

export function Hero({ anyRunning, scenarioCount, history }: Props) {
  const successes = history.filter((h) => h.status === "succeeded").length;
  const avgDurationSec =
    history.length > 0
      ? history.reduce((s, h) => s + (h.duration_sec ?? 0), 0) / history.length
      : 0;

  const kpis = [
    { label: "전체 시나리오", value: scenarioCount, hint: "pre-defined" },
    { label: "금일 실행", value: history.length, hint: `성공 ${successes}` },
    {
      label: "평균 소요",
      value: history.length > 0 ? fmtTime(avgDurationSec) : "—",
      hint: "mm:ss",
    },
  ];

  return (
    <section className="max-w-[1440px] mx-auto px-6 pt-10 pb-6">
      <div className="flex items-end justify-between gap-6">
        <div>
          <div className="flex items-center gap-2 mb-3">
            <span className="inline-flex items-center gap-1.5 rounded-full bg-white/80 ring-1 hair px-2.5 py-1 text-[11px] text-[var(--ink-2)]">
              <span
                className={`w-1.5 h-1.5 rounded-full ${
                  anyRunning
                    ? "bg-violet-500 pulse-run"
                    : "bg-emerald-500 pulse-ok"
                }`}
              />
              {anyRunning ? "시나리오 실행 중" : "실행 중인 시나리오 없음"}
            </span>
            <span className="mono text-[11px] text-[var(--ink-3)]">
              · {today()}
            </span>
          </div>
          <h1 className="text-[32px] leading-[1.15] font-semibold tracking-[-0.02em] text-[var(--ink)]">
            Scenario Runner
          </h1>
          <p
            className="mt-1.5 text-[13.5px] text-[var(--ink-2)] max-w-[640px] leading-relaxed"
            style={{ textWrap: "pretty" } as React.CSSProperties}
          >
            RCA testbed 시나리오를 실행할 수 있는 공간입니다. 한 번에 하나의
            시나리오만 실행되며, cleanup 은 언제든 idempotent 하게 호출할 수
            있습니다.
          </p>
        </div>

        <div className="hidden lg:grid grid-cols-3 gap-3 shrink-0">
          {kpis.map((k, i) => (
            <div key={i} className="card px-4 py-3 min-w-[130px]">
              <div className="mono text-[10px] uppercase tracking-[0.15em] text-[var(--ink-3)]">
                {k.label}
              </div>
              <div className="mt-1 flex items-baseline gap-1.5">
                <div className="text-[26px] font-semibold tracking-[-0.02em] text-[var(--ink)] tabular-nums">
                  {k.value}
                </div>
                <div className="mono text-[10.5px] text-[var(--ink-3)]">
                  {k.hint}
                </div>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}
