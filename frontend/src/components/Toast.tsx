import { Icon } from "./Icon";

export type ToastKind = "succeeded" | "failed" | "cleanup" | "info";

export interface ToastData {
  kind: ToastKind;
  title: string;
  subtitle: string;
}

interface Props {
  toast: ToastData | null;
}

export function Toast({ toast }: Props) {
  if (!toast) return null;

  const ringClass =
    toast.kind === "succeeded"
      ? "ring-emerald-200"
      : toast.kind === "failed"
        ? "ring-rose-200"
        : toast.kind === "cleanup"
          ? "ring-violet-200"
          : "ring-slate-200";

  const iconBg =
    toast.kind === "succeeded"
      ? "bg-emerald-50 text-emerald-600"
      : toast.kind === "failed"
        ? "bg-rose-50 text-rose-600"
        : toast.kind === "cleanup"
          ? "bg-violet-50 text-violet-600"
          : "bg-slate-50 text-slate-600";

  const iconName =
    toast.kind === "succeeded"
      ? "check"
      : toast.kind === "failed"
        ? "x"
        : toast.kind === "cleanup"
          ? "broom"
          : "dots";

  return (
    <div className="fixed bottom-6 right-6 z-50 slide-up">
      <div
        className={`flex items-center gap-3 pl-3 pr-4 py-3 rounded-xl ring-1 bg-white shadow-xl min-w-[280px] ${ringClass}`}
      >
        <div
          className={`w-8 h-8 rounded-lg flex items-center justify-center ${iconBg}`}
        >
          <Icon name={iconName} className="w-4 h-4" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-medium text-[var(--ink)] truncate">
            {toast.title}
          </div>
          <div className="text-[11.5px] text-[var(--ink-3)] truncate mono">
            {toast.subtitle}
          </div>
        </div>
      </div>
    </div>
  );
}
