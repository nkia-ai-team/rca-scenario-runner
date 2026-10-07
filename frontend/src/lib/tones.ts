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

// 현행(manifest) 시나리오 id — "F04-H". 숫자는 장애 계열, 접미사는 변형이다
// (R 같은 원인에 다른 대상 / H 같은 증상에 다른 원인 / P 부분 유사 / 그 외 G, S, T, Q).
const LIVE_ID = /^F(\d+)-([A-Z])/;
const VARIANT_TONE: Record<string, Tone> = {
  R: "violet",
  H: "amber",
  P: "emerald",
};

export interface Presentation {
  tone: Tone;
  tag: string;
  num: string;
  // 화면에 찍는 식별 코드 — 레거시는 "S01", 현행은 id 그대로("F04-H").
  code: string;
}

export function presentationFor(scenarioId: string): Presentation {
  const live = LIVE_ID.exec(scenarioId);
  if (live) {
    return {
      tone: VARIANT_TONE[live[2]] ?? "rose",
      tag: "Scenario",
      num: live[1],
      code: scenarioId,
    };
  }
  const legacy = PRESENTATION[scenarioId];
  if (legacy) return { ...legacy, code: `S${legacy.num}` };
  return {
    tone: "violet",
    tag: "Scenario",
    num: scenarioId,
    code: `S${scenarioId}`,
  };
}
