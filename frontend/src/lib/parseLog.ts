import type { LogLine } from "../types";

const ANSI = /\x1b\[[0-9;]*m/g;
const LEADING_TAG =
  /^\[(INFO|WARN|WARNING|ERROR|ERR|OK|DEBUG)\]\s*(.*)$/i;
const TIMESTAMP =
  /^(\d{4}-\d{2}-\d{2}\s+(\d{2}:\d{2}:\d{2}(?:\.\d+)?))\s+(.*)$/;

function normalizeLevel(raw: string): LogLine["lvl"] {
  const u = raw.toUpperCase();
  if (u === "ERROR" || u === "ERR") return "error";
  if (u === "WARN" || u === "WARNING") return "warn";
  if (u === "DEBUG") return "debug";
  return "info";
}

export function parseLogLine(raw: string, index: number): LogLine {
  const clean = raw.replace(ANSI, "").replace(/\r$/, "");
  let lvl: LogLine["lvl"] = "info";
  let rest = clean;

  const tag = rest.match(LEADING_TAG);
  if (tag) {
    lvl = normalizeLevel(tag[1]);
    rest = tag[2];
  }

  let t = "";
  const ts = rest.match(TIMESTAMP);
  if (ts) {
    t = ts[2];
    rest = ts[3];
  }

  const svcMatch = rest.match(/^\[([A-Za-z0-9_\-./]+)\]\s*(.*)$/);
  let svc = "runner";
  let msg = rest;
  if (svcMatch) {
    svc = svcMatch[1];
    msg = svcMatch[2];
  }

  return { i: index, t, lvl, svc, msg: msg.trim() };
}

export function parseLogTail(tail: string[], startIndex = 0): LogLine[] {
  return tail.map((line, i) => parseLogLine(line, startIndex + i + 1));
}
