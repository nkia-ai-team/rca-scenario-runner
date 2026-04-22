import type { ApiScenario, HistoryEntry, RunInfo } from "../types";

class ApiError extends Error {
  status: number;
  detail?: string;
  constructor(status: number, detail?: string) {
    super(`API ${status}: ${detail ?? ""}`.trim());
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const res = await fetch(path, {
    headers: { "content-type": "application/json", ...(init.headers ?? {}) },
    ...init,
  });
  if (!res.ok) {
    let detail: string | undefined;
    try {
      const body = await res.json();
      detail = body?.detail ?? body?.error;
    } catch {
      // non-json body
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  healthz: () => request<{ status: string; version: string }>("/healthz"),
  listScenarios: () => request<ApiScenario[]>("/api/scenarios"),
  run: (id: string) =>
    request<RunInfo>(`/api/scenarios/${id}/run`, { method: "POST" }),
  cleanup: (id: string) =>
    request<RunInfo>(`/api/scenarios/${id}/cleanup`, { method: "POST" }),
  status: (id: string) =>
    request<RunInfo>(`/api/scenarios/${id}/status`),
  fullLog: (id: string, runId: string) =>
    fetch(
      `/api/scenarios/${id}/logs?run_id=${encodeURIComponent(runId)}`,
    ).then((r) => (r.ok ? r.text() : Promise.reject(new ApiError(r.status)))),
  history: () => request<HistoryEntry[]>("/api/history"),
};

export { ApiError };
