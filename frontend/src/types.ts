export type Status =
  | "idle"
  | "running"
  | "succeeded"
  | "failed"
  | "cleanup_running";

export type DisplayStatus = "idle" | "running" | "succeeded" | "failed";

export type ScenarioMode = "run" | "cleanup";

export interface ApiScenario {
  id: string;
  name: string;
  description: string;
  cause: string;
  propagation: string;
  expected_alarms: string[];
  estimated_duration_sec: number;
  script_filename: string;
  warnings: string[];
}

export interface RunInfo {
  run_id: string;
  scenario_id: string;
  mode: ScenarioMode;
  status: Status;
  started_at: string;
  finished_at: string | null;
  exit_code: number | null;
  log_tail: string[];
}

export interface HistoryEntry {
  run_id: string;
  scenario_id: string;
  mode: ScenarioMode;
  status: Status;
  started_at: string;
  finished_at: string | null;
  duration_sec: number | null;
  exit_code: number | null;
}

export type Tone = "violet" | "amber" | "emerald" | "rose";

export interface ScenarioView extends ApiScenario {
  num: string;
  tone: Tone;
  tag: string;
  propagationHops: string[];
}

export interface LogLine {
  i: number;
  t: string;
  lvl: "info" | "warn" | "error" | "debug";
  svc: string;
  msg: string;
}

export interface HistoryView extends HistoryEntry {
  scenario: ScenarioView;
  elapsed: number;
  result: DisplayStatus;
}
