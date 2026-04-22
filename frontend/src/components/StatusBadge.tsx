import type { DisplayStatus } from "../types";

const MAP: Record<
  DisplayStatus,
  { cls: string; dot: string; label: string }
> = {
  idle: {
    cls: "bg-slate-50 text-slate-600 ring-slate-200",
    dot: "bg-slate-400",
    label: "Idle",
  },
  running: {
    cls: "bg-violet-50 text-violet-700 ring-violet-200",
    dot: "bg-violet-500 pulse-run",
    label: "Running",
  },
  succeeded: {
    cls: "bg-emerald-50 text-emerald-700 ring-emerald-200",
    dot: "bg-emerald-500",
    label: "Succeeded",
  },
  failed: {
    cls: "bg-rose-50 text-rose-700 ring-rose-200",
    dot: "bg-rose-500",
    label: "Failed",
  },
};

interface Props {
  status: DisplayStatus;
}

export function StatusBadge({ status }: Props) {
  const m = MAP[status];
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full ring-1 px-2 py-0.5 text-[11px] font-medium ${m.cls}`}
    >
      <span className={`w-1.5 h-1.5 rounded-full ${m.dot}`} />
      {m.label}
    </span>
  );
}
