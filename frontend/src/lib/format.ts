export const fmtTime = (s: number): string => {
  const total = Math.max(0, Math.floor(s));
  const m = Math.floor(total / 60);
  const ss = total % 60;
  return `${String(m).padStart(2, "0")}:${String(ss).padStart(2, "0")}`;
};

export const fmtStamp = (d: Date): string => {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(
    d.getHours(),
  )}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};

export const relTime = (d: Date): string => {
  const s = Math.floor((Date.now() - d.getTime()) / 1000);
  if (s < 60) return `${s}초 전`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}분 전`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}시간 전`;
  return `${Math.floor(h / 24)}일 전`;
};

export const durationSec = (from: Date, to: Date): number =>
  Math.max(0, (to.getTime() - from.getTime()) / 1000);
