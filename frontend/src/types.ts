export type Status =
  | "idle"
  | "running"
  | "succeeded"
  | "failed"
  | "cleanup_running";

export type DisplayStatus = "idle" | "running" | "succeeded" | "failed";

export type ScenarioMode = "run" | "cleanup";

export interface ExecutionSpec {
  transport: "local" | "ssh" | "docker" | "kubectl" | "api";
  location: string;
  timeout_sec: number;
  host: string | null;
  user: string | null;
  port: number;
  identity_file: string | null;
  container: string | null;
  namespace: string | null;
  resource: string | null;
  url: string | null;
  cleanup_url: string | null;
  header_env: Record<string, string>;
}

export interface InjectionPoint extends ExecutionSpec {
  id: string;
  kind: "north_south" | "east_west" | "database" | "node_resource" |
    "container_resource" | "external_mock" | "network_path" | "change" |
    "business_fault" | "composite_control";
  target: string;
  entry_path: string;
  cleanup_location: string;
  rationale: string;
  feasibility: "ready" | "calibrate" | "prerequisite" | "defer";
  managed_by: "runner" | "orchestrator";
  script: string | null;
}

export interface ExecutionPlan {
  orchestrator: ExecutionSpec;
  injection_points: InjectionPoint[];
}

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
  execution: ExecutionPlan;
  warnings: string[];
  // RCA ground-truth (optional; populated from service-spec.yaml)
  // 1~5 — 5 = 결정적, 1 = 추정만 가능
  difficulty: number | null;
  // 관측 가능한 시그널 기반 채점 기준. UI 의 "RCA 채점 기준" 섹션에 표시.
  expected_rca_root_cause: string | null;
  expected_clusters: Record<string, unknown> | null;
  expected_incidents: Record<string, unknown> | null;
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
