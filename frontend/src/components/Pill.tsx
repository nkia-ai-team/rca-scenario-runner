import type { ReactNode } from "react";

import { TONE } from "../lib/tones";
import type { Tone } from "../types";

type PillTone = Tone | "neutral";

interface Props {
  children: ReactNode;
  tone?: PillTone;
}

const NEUTRAL = "bg-slate-50 text-slate-700 ring-slate-200";

export function Pill({ children, tone = "neutral" }: Props) {
  const cls = tone === "neutral" ? NEUTRAL : TONE[tone].chip;
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full ring-1 px-2 py-0.5 text-[11px] font-medium ${cls}`}
    >
      {children}
    </span>
  );
}
