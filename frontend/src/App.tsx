import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { ExecutionPanel } from "./components/ExecutionPanel";
import { Hero } from "./components/Hero";
import { HistoryTable } from "./components/HistoryTable";
import { Icon } from "./components/Icon";
import { ScenarioCard } from "./components/ScenarioCard";
import { Toast, type ToastData } from "./components/Toast";
import { TopBar } from "./components/TopBar";
import { useActiveRun } from "./hooks/useActiveRun";
import { useHealth } from "./hooks/useHealth";
import { useRunner } from "./hooks/useRunner";
import { useScenarios } from "./hooks/useScenarios";
import { durationSec } from "./lib/format";
import type { DisplayStatus } from "./types";

const DEFAULT_DOMAIN_SLUG = "plopvape-shop";

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
  const activeRun = useActiveRun();

  // Derive domain tabs from the loaded scenarios (one /api/scenarios call
  // already covers it — avoids a second /api/domains roundtrip).
  const domains = (() => {
    const acc = new Map<string, { slug: string; label: string; scenario_count: number }>();
    for (const s of scenarios) {
      const cur = acc.get(s.domain);
      if (cur) cur.scenario_count += 1;
      else acc.set(s.domain, { slug: s.domain, label: s.domain_label, scenario_count: 1 });
    }
    return Array.from(acc.values()).sort((a, b) => a.slug.localeCompare(b.slug));
  })();

  // Initial selection: plopvape-shop if present (preserves what the team is
  // already using); otherwise the first domain alphabetically.
  const [selectedDomain, setSelectedDomain] = useState<string | null>(null);
  useEffect(() => {
    if (selectedDomain !== null) return;
    if (domains.length === 0) return;
    const preferred = domains.find((d) => d.slug === DEFAULT_DOMAIN_SLUG);
    setSelectedDomain(preferred?.slug ?? domains[0].slug);
  }, [domains, selectedDomain]);

  const visibleScenarios = selectedDomain
    ? scenarios.filter((s) => s.domain === selectedDomain)
    : scenarios;

  // Concurrency banner — only show when someone *else* is running.
  // "Someone else" = the active run_id is not what this tab launched.
  const myRunId = runner.exec?.runId ?? null;
  const someoneElseRunning =
    activeRun.is_active &&
    activeRun.run_id !== null &&
    activeRun.run_id !== myRunId;
  const occupiedScenario = someoneElseRunning && activeRun.scenario_id
    ? scenarios.find((s) => s.id === activeRun.scenario_id)
    : undefined;

  const [historyOpen, setHistoryOpen] = useState(true);
  const [toast, setToast] = useState<ToastData | null>(null);
  const prevStatusRef = useRef<DisplayStatus | null>(null);
  const leftSectionRef = useRef<HTMLElement | null>(null);
  const [leftHeight, setLeftHeight] = useState<number | null>(null);

  // 왼쪽 시나리오 리스트 높이를 추적해 오른쪽 실행 패널 max-height 로 사용한다.
  // 카드 펼침/접힘마다 높이가 변하므로 ResizeObserver 로 동기화. CSS sibling
  // 높이 매칭은 순수하게 불가능해서 JS 가 유일한 깔끔한 경로.
  useLayoutEffect(() => {
    const el = leftSectionRef.current;
    if (!el) return;
    const update = () =>
      setLeftHeight(Math.round(el.getBoundingClientRect().height));
    update();
    const obs = new ResizeObserver(update);
    obs.observe(el);
    return () => obs.disconnect();
  }, []);

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
        <TopBar
          backendOk={backendOk}
          domains={domains}
          selectedDomain={selectedDomain}
          onSelectDomain={setSelectedDomain}
        />

        <Hero
          anyRunning={runner.anyRunning}
          scenarioCount={visibleScenarios.length}
          history={runner.history}
        />

        {someoneElseRunning && (
          <div className="max-w-[1440px] mx-auto px-6 pt-3">
            <div className="card p-3 fade-in flex items-center gap-3 ring-1 ring-amber-200 bg-amber-50/60">
              <span className="w-2 h-2 rounded-full bg-amber-500 animate-pulse" />
              <div className="text-[12.5px] text-amber-900">
                다른 사용자가 시나리오 실행 중 — 새 실행은 동시에 불가합니다.
                {occupiedScenario && (
                  <span className="ml-1 text-amber-800">
                    · {occupiedScenario.domain_label} / {occupiedScenario.name}
                    {" "}
                    <span className="mono text-[11px] opacity-75">
                      (S{occupiedScenario.num})
                    </span>
                  </span>
                )}
              </div>
            </div>
          </div>
        )}

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

          <div className="grid grid-cols-5 gap-5 items-start">
            <section ref={leftSectionRef} className="col-span-2 space-y-3">
              <div className="flex items-baseline justify-between px-1">
                <div>
                  <h2 className="text-[15px] font-semibold text-[var(--ink)] tracking-[-0.01em]">
                    시나리오
                  </h2>
                  <p className="text-[11.5px] text-[var(--ink-3)] mt-0.5">
                    {selectedDomain && domains.find((d) => d.slug === selectedDomain)?.label}
                    {" · "}
                    {visibleScenarios.length}건 · cleanup 멱등 보장
                  </p>
                </div>
              </div>
              <div className="space-y-3">
                {loading && (
                  <div className="card p-6 text-center text-[13px] text-[var(--ink-3)]">
                    시나리오 불러오는 중…
                  </div>
                )}
                {!loading && visibleScenarios.length === 0 && selectedDomain && (
                  <div className="card p-6 text-center text-[13px] text-[var(--ink-3)]">
                    이 도메인에 등록된 시나리오가 없습니다.
                  </div>
                )}
                {visibleScenarios.map((s) => (
                  <ScenarioCard
                    key={s.id}
                    scn={s}
                    status={runner.statuses[s.id] ?? "idle"}
                    runDisabled={
                      someoneElseRunning ||
                      (runner.anyRunning &&
                        runner.statuses[s.id] !== "running")
                    }
                    isSelected={selectedId === s.id}
                    onRun={runner.run}
                    onCleanup={runner.cleanup}
                  />
                ))}
              </div>
            </section>

            <section
              className="col-span-3 sticky top-[72px] self-start overflow-hidden"
              style={{
                // height (not max-height) 라야 자식 ExecutionPanel 의 h-full
                // 이 실제 픽셀로 해석되고, 그 안의 flex-1 min-h-0 체인이
                // LogViewer 의 overflow-y-auto 를 트리거할 수 있다.
                height: leftHeight
                  ? `min(${leftHeight}px, calc(100vh - 96px))`
                  : "calc(100vh - 96px)",
              }}
            >
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
