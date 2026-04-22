import { useEffect, useRef, useState } from "react";

import { ExecutionPanel } from "./components/ExecutionPanel";
import { Hero } from "./components/Hero";
import { HistoryTable } from "./components/HistoryTable";
import { Icon } from "./components/Icon";
import { ScenarioCard } from "./components/ScenarioCard";
import { Toast, type ToastData } from "./components/Toast";
import { TopBar } from "./components/TopBar";
import { useHealth } from "./hooks/useHealth";
import { useRunner } from "./hooks/useRunner";
import { useScenarios } from "./hooks/useScenarios";
import { durationSec } from "./lib/format";
import type { DisplayStatus } from "./types";

function useElapsed(
  status: DisplayStatus,
  startedAt: Date | null,
  finishedAt: Date | null,
): number {
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (status !== "running") return;
    const id = setInterval(() => setTick((n) => n + 1), 250);
    return () => clearInterval(id);
  }, [status]);

  if (!startedAt) return 0;
  if (status === "running") {
    return durationSec(startedAt, new Date());
  }
  const end = finishedAt ?? new Date();
  return durationSec(startedAt, end);
  void tick; // keep dep-tracked re-render while running
}

export default function App() {
  const { scenarios, loading, error: scenariosError } = useScenarios();
  const backendOk = useHealth();
  const runner = useRunner(scenarios);

  const [historyOpen, setHistoryOpen] = useState(true);
  const [toast, setToast] = useState<ToastData | null>(null);
  const prevStatusRef = useRef<DisplayStatus | null>(null);

  const elapsed = useElapsed(
    runner.exec?.status ?? "idle",
    runner.exec?.startedAt ?? null,
    runner.exec?.finishedAt ?? null,
  );

  useEffect(() => {
    if (!runner.exec) {
      prevStatusRef.current = null;
      return;
    }
    const prev = prevStatusRef.current;
    const cur = runner.exec.status;
    const mode = runner.exec.mode;

    if (prev === "running" && (cur === "succeeded" || cur === "failed")) {
      if (mode === "cleanup") {
        setToast({
          kind: cur === "succeeded" ? "cleanup" : "failed",
          title:
            cur === "succeeded"
              ? `${runner.exec.scenario.name} cleanup 완료`
              : `${runner.exec.scenario.name} cleanup 실패`,
          subtitle: `S${runner.exec.scenario.num} · exit ${runner.exec.exitCode ?? "?"}`,
        });
      } else {
        setToast({
          kind: cur,
          title:
            cur === "succeeded"
              ? `${runner.exec.scenario.name} 실행 성공`
              : `${runner.exec.scenario.name} 실행 실패`,
          subtitle: `S${runner.exec.scenario.num} · exit ${runner.exec.exitCode ?? "?"}`,
        });
      }
      const timer = setTimeout(() => setToast(null), 3200);
      prevStatusRef.current = cur;
      return () => clearTimeout(timer);
    }

    prevStatusRef.current = cur;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runner.exec?.status, runner.exec?.mode, runner.exec?.exitCode]);

  useEffect(() => {
    if (!runner.error) return;
    setToast({
      kind: "failed",
      title: "오류",
      subtitle: runner.error,
    });
    const t = setTimeout(() => setToast(null), 3200);
    return () => clearTimeout(t);
  }, [runner.error]);

  const selectedId = runner.exec?.scenario.id;

  return (
    <div className="min-h-screen relative">
      <div className="absolute inset-x-0 top-0 h-[380px] aurora pointer-events-none" />
      <div
        className="absolute inset-0 dot-bg pointer-events-none"
        style={{
          opacity: 0.45,
          maskImage: "linear-gradient(to bottom, black, transparent 60%)",
          WebkitMaskImage: "linear-gradient(to bottom, black, transparent 60%)",
        }}
      />

      <div className="relative">
        <TopBar backendOk={backendOk} />

        <Hero
          anyRunning={runner.anyRunning}
          scenarioCount={scenarios.length}
          history={runner.history}
        />

        <main className="max-w-[1440px] mx-auto px-6 pb-10 space-y-5">
          {scenariosError && (
            <div className="card p-4 fade-in">
              <div className="text-[13px] text-rose-700">
                시나리오 목록을 불러오지 못했습니다 — {scenariosError}
              </div>
              <div className="text-[11.5px] text-[var(--ink-3)] mt-1">
                backend 가 실행 중인지 확인하세요:
                <span className="mono"> uv run uvicorn app.main:app --port 8000</span>
              </div>
            </div>
          )}

          <div className="grid grid-cols-5 gap-5">
            <section className="col-span-2 space-y-3">
              <div className="flex items-baseline justify-between px-1">
                <div>
                  <h2 className="text-[15px] font-semibold text-[var(--ink)] tracking-[-0.01em]">
                    시나리오
                  </h2>
                  <p className="text-[11.5px] text-[var(--ink-3)] mt-0.5">
                    사전 정의 {scenarios.length}건 · cleanup 멱등 보장
                  </p>
                </div>
              </div>
              <div className="space-y-3">
                {loading && (
                  <div className="card p-6 text-center text-[13px] text-[var(--ink-3)]">
                    시나리오 불러오는 중…
                  </div>
                )}
                {scenarios.map((s) => (
                  <ScenarioCard
                    key={s.id}
                    scn={s}
                    status={runner.statuses[s.id] ?? "idle"}
                    runDisabled={
                      runner.anyRunning &&
                      runner.statuses[s.id] !== "running"
                    }
                    isSelected={selectedId === s.id}
                    onRun={runner.run}
                    onCleanup={runner.cleanup}
                  />
                ))}
              </div>
            </section>

            <section className="col-span-3 max-h-[calc(100vh-180px)]">
              <ExecutionPanel
                exec={runner.exec}
                elapsed={elapsed}
                onCopy={runner.copyLog}
                onDownload={runner.downloadLog}
              />
            </section>
          </div>

          <HistoryTable
            history={runner.history}
            scenarios={scenarios}
            open={historyOpen}
            setOpen={setHistoryOpen}
          />

          <footer className="pt-4 pb-8 flex items-center justify-between text-[11px] text-[var(--ink-3)]">
            <div className="flex items-center gap-2">
              <Icon name="shield" className="w-3.5 h-3.5" />
              <span>내부 전용 도구 · 외부 노출 금지</span>
            </div>
            <div className="flex items-center gap-4">
              <span>개선사항은 AI1팀 방성준에게 말씀해주세요</span>
            </div>
          </footer>
        </main>
      </div>

      <Toast toast={toast} />
    </div>
  );
}
