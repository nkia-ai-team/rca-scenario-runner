export type Status =
  | "idle"
  | "running"
  | "succeeded"
  | "failed"
  | "cleanup_running";

export type DisplayStatus = "idle" | "running" | "succeeded" | "failed";

export type ScenarioMode = "run" | "cleanup";

export interface ApiScenario {
  id: string;            // composite "<domain>:<short_id>"
  short_id: string;      // within-domain id, e.g. "01"
  domain: string;        // folder slug
  domain_label: string;  // human-readable
  name: string;
  description: string;
  cause: string;
  propagation: string;
  expected_alarms: string[];
  estimated_duration_sec: number;
  script_filename: string;
  warnings: string[];
}

export interface Domain {
  slug: string;
  label: string;
  scenario_count: number;
}

export interface ActiveRun {
  is_active: boolean;
  scenario_id: string | null;
  run_id: string | null;
  mode: ScenarioMode | null;
  started_at: string | null;
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
