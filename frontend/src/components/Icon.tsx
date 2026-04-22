import type { ReactNode } from "react";

export type IconName =
  | "play"
  | "chev"
  | "arr"
  | "copy"
  | "down"
  | "check"
  | "x"
  | "bolt"
  | "broom"
  | "eye"
  | "dots"
  | "bell"
  | "sparkle"
  | "shield"
  | "split"
  | "search"
  | "spinner";

const PATHS: Record<IconName, ReactNode> = {
  play: <path d="M7 5l12 7-12 7V5z" />,
  chev: <path d="M6 9l6 6 6-6" />,
  arr: <path d="M5 12h14M13 6l6 6-6 6" />,
  copy: (
    <>
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path d="M5 15V5a2 2 0 012-2h10" />
    </>
  ),
  down: <path d="M12 4v12m-5-5l5 5 5-5M4 20h16" />,
  check: <path d="M5 12l5 5 10-11" />,
  x: <path d="M6 6l12 12M18 6L6 18" />,
  bolt: <path d="M13 2L3 14h7l-1 8 10-12h-7l1-8z" />,
  broom: (
    <path d="M14 3l7 7M11 6l7 7-5 5a4 4 0 01-5.7 0l-1.3-1.3a4 4 0 010-5.7l5-5zM3 21l6-2" />
  ),
  eye: (
    <>
      <path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z" />
      <circle cx="12" cy="12" r="3" />
    </>
  ),
  dots: (
    <>
      <circle cx="5" cy="12" r="1.3" />
      <circle cx="12" cy="12" r="1.3" />
      <circle cx="19" cy="12" r="1.3" />
    </>
  ),
  bell: (
    <>
      <path d="M6 8a6 6 0 1112 0c0 7 3 9 3 9H3s3-2 3-9z" />
      <path d="M10 21a2 2 0 004 0" />
    </>
  ),
  sparkle: (
    <path d="M12 3v6m0 6v6m-9-9h6m6 0h6M6 6l3 3m6 6l3 3m0-12l-3 3m-6 6l-3 3" />
  ),
  shield: <path d="M12 2l8 3v6c0 5-3.5 9-8 11-4.5-2-8-6-8-11V5l8-3z" />,
  split: <path d="M5 6h3l8 12h3M5 18h3l3-4.5M19 6h-3" />,
  search: (
    <>
      <circle cx="11" cy="11" r="7" />
      <path d="M21 21l-4.3-4.3" />
    </>
  ),
  // Faint full circle + 90° accent arc. Rotated via `.spin` class to give a
  // classic loading look without being as busy as the bolt icon.
  spinner: (
    <>
      <circle cx="12" cy="12" r="9" opacity="0.2" />
      <path d="M21 12a9 9 0 0 0-9-9" />
    </>
  ),
};

interface Props {
  name: IconName;
  className?: string;
}

export function Icon({ name, className = "w-4 h-4" }: Props) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
    >
      {PATHS[name]}
    </svg>
  );
}
