import { useEffect, useRef, useState } from "react";

import type { LogLine } from "../types";
import { Icon } from "./Icon";

type Filter = "all" | LogLine["lvl"];

const LVL: Record<LogLine["lvl"], string> = {
  info: "text-slate-300",
  warn: "text-amber-300",
  error: "text-rose-300",
  debug: "text-slate-500",
};

interface Props {
  lines: LogLine[];
  onCopy: () => void | Promise<void>;
  onDownload: () => void | Promise<void>;
}

export function LogViewer({ lines, onCopy, onDownload }: Props) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [autoscroll, setAutoscroll] = useState(true);
  const [copyState, setCopyState] = useState<"idle" | "done">("idle");
  const [filter, setFilter] = useState<Filter>("all");

  useEffect(() => {
    if (autoscroll && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [lines, autoscroll, filter]);

  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 20;
    setAutoscroll(atBottom);
  };

  const handleCopy = async () => {
    await onCopy();
    setCopyState("done");
    setTimeout(() => setCopyState("idle"), 1200);
  };

  const filtered = filter === "all" ? lines : lines.filter((l) => l.lvl === filter);
  const shown = filtered.slice(-200);

  const counts: Record<LogLine["lvl"], number> = {
    info: 0,
    warn: 0,
    error: 0,
    debug: 0,
  };
  lines.forEach((l) => {
    counts[l.lvl] = (counts[l.lvl] ?? 0) + 1;
  });

  return (
    <div className="terminal terminal-hair flex flex-col overflow-hidden flex-1 min-h-0 relative">
      <div className="px-3 py-2 flex items-center justify-between border-b border-[oklch(24%_0.015_270)]">
        <div className="flex items-center gap-2.5">
          <div className="flex items-center gap-1">
            <span className="w-2.5 h-2.5 rounded-full bg-rose-400/80" />
            <span className="w-2.5 h-2.5 rounded-full bg-amber-400/80" />
            <span className="w-2.5 h-2.5 rounded-full bg-emerald-400/80" />
          </div>
          <div className="mono text-[11px] text-slate-300 ml-1">runner.log</div>
          <div className="mono text-[10.5px] text-slate-500">
            · {lines.length} 줄
          </div>
        </div>
        <div className="flex items-center gap-1">
          {(["all", "info", "warn", "error"] as Filter[]).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`mono text-[10.5px] uppercase tracking-wider px-1.5 py-0.5 rounded ${
                filter === f
                  ? "bg-white/10 text-slate-100"
                  : "text-slate-500 hover:text-slate-300"
              }`}
            >
              {f}
              {f !== "all" && counts[f as LogLine["lvl"]]
                ? ` ${counts[f as LogLine["lvl"]]}`
                : ""}
            </button>
          ))}
          <div className="w-px h-4 bg-white/10 mx-1" />
          <button
            onClick={handleCopy}
            className="tip inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10.5px] mono text-slate-400 hover:text-slate-100 hover:bg-white/5"
            data-tip="전체 로그 복사"
          >
            <Icon name="copy" className="w-3 h-3" />
            {copyState === "done" ? "copied" : "copy"}
          </button>
          <button
            onClick={onDownload}
            className="tip inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10.5px] mono text-slate-400 hover:text-slate-100 hover:bg-white/5"
            data-tip="전체 로그 파일 다운로드"
          >
            <Icon name="down" className="w-3 h-3" />
            save
          </button>
        </div>
      </div>

      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="log-scroll flex-1 min-h-0 overflow-y-auto mono text-[12px] leading-[1.6] py-2"
      >
        {shown.length === 0 ? (
          <div className="px-3 py-10 text-slate-500 text-center">
            <div className="mono text-[11px] uppercase tracking-wider">
              대기 중 · waiting for scenario
            </div>
            <div className="text-[11px] text-slate-600 mt-1">
              시나리오를 실행하면 stdout 과 telemetry 가 여기에 스트리밍됩니다
            </div>
          </div>
        ) : (
          shown.map((ln) => (
            <div
              key={ln.i}
              className="px-3 flex items-start gap-3 hover:bg-white/[0.03]"
            >
              <span className="text-slate-600 select-none w-8 text-right">
                {String(ln.i).padStart(4, "0")}
              </span>
              <span className="text-slate-500 shrink-0">{ln.t || "—"}</span>
              <span className={`shrink-0 uppercase w-10 ${LVL[ln.lvl]}`}>
                {ln.lvl}
              </span>
              <span className="shrink-0 text-violet-300/90 w-[110px] truncate">
                {ln.svc}
              </span>
              <span className={`${LVL[ln.lvl]} break-all`}>{ln.msg}</span>
            </div>
          ))
        )}
      </div>

      {!autoscroll && (
        <button
          onClick={() => {
            setAutoscroll(true);
            if (scrollRef.current) {
              scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
            }
          }}
          className="absolute bottom-4 right-6 z-10 inline-flex items-center gap-1.5 bg-white/10 hover:bg-white/20 text-slate-100 backdrop-blur text-[11px] mono px-2.5 py-1 rounded-full border border-white/10"
        >
          <Icon name="chev" className="w-3 h-3" /> 맨 아래로
        </button>
      )}
    </div>
  );
}
