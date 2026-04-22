import type { Tone } from "../types";

interface ToneClasses {
  chip: string;
  dot: string;
  soft: string;
  ring: string;
  text: string;
}

export const TONE: Record<Tone, ToneClasses> = {
  violet: {
    chip: "bg-violet-50 text-violet-700 ring-violet-200",
    dot: "bg-violet-500",
    soft: "bg-violet-50",
    ring: "ring-violet-200",
    text: "text-violet-700",
  },
  amber: {
    chip: "bg-amber-50 text-amber-800 ring-amber-200",
    dot: "bg-amber-500",
    soft: "bg-amber-50",
    ring: "ring-amber-200",
    text: "text-amber-800",
  },
  emerald: {
    chip: "bg-emerald-50 text-emerald-700 ring-emerald-200",
    dot: "bg-emerald-500",
    soft: "bg-emerald-50",
    ring: "ring-emerald-200",
    text: "text-emerald-700",
  },
  rose: {
    chip: "bg-rose-50 text-rose-700 ring-rose-200",
    dot: "bg-rose-500",
    soft: "bg-rose-50",
    ring: "ring-rose-200",
    text: "text-rose-700",
  },
};

const PRESENTATION: Record<string, { tone: Tone; tag: string; num: string }> = {
  "01": { tone: "violet", tag: "Database", num: "01" },
  "02": { tone: "amber", tag: "Network", num: "02" },
  "03": { tone: "emerald", tag: "Kubernetes", num: "03" },
  "04": { tone: "rose", tag: "Load", num: "04" },
};

export function presentationFor(scenarioId: string) {
  return (
    PRESENTATION[scenarioId] ?? {
      tone: "violet" as Tone,
      tag: "Scenario",
      num: scenarioId,
    }
  );
}
