import { Icon } from "./Icon";

interface DomainTab {
  slug: string;
  label: string;
  scenario_count: number;
}

interface Props {
  backendOk: boolean;
  envTag?: string;
  domains: DomainTab[];
  selectedDomain: string | null;
  onSelectDomain: (slug: string) => void;
}

export function TopBar({
  backendOk,
  envTag = "testbed-109",
  domains,
  selectedDomain,
  onSelectDomain,
}: Props) {
  return (
    <header className="sticky top-0 z-40 border-b hair bg-[var(--bg)]/70 backdrop-blur-md">
      <div className="max-w-[1440px] mx-auto px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-[var(--ink)] flex items-center justify-center text-white relative overflow-hidden">
            <svg
              viewBox="0 0 24 24"
              className="w-4 h-4"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.8"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M4 13l4-4 4 6 8-10" />
              <circle cx="4" cy="13" r="1.5" fill="currentColor" />
            </svg>
          </div>
          <div className="leading-tight">
            <div className="flex items-baseline gap-2">
              <span className="text-[14px] font-semibold text-[var(--ink)] tracking-[-0.01em]">
                RCA Testbed
              </span>
              <span className="mono text-[10px] text-[var(--ink-3)]">v1.0</span>
            </div>
            <div className="text-[11px] text-[var(--ink-2)]">
              Scenario Runner · 내부 QA 도구
            </div>
          </div>
          <nav
            className="ml-6 hidden md:flex items-center gap-0.5 p-0.5 rounded-lg ring-1 hair bg-white/60"
            aria-label="testbed domain selector"
          >
            {domains.length === 0 && (
              <span className="text-[12px] px-2.5 py-1 text-[var(--ink-3)]">
                도메인 로드 중…
              </span>
            )}
            {domains.map((d) => {
              const active = d.slug === selectedDomain;
              return (
                <button
                  key={d.slug}
                  onClick={() => onSelectDomain(d.slug)}
                  className={`text-[12px] px-2.5 py-1 rounded-md transition flex items-center gap-1.5 ${
                    active
                      ? "bg-white text-[var(--ink)] shadow-sm ring-1 ring-[var(--hair)]"
                      : "text-[var(--ink-3)] hover:text-[var(--ink)]"
                  }`}
                  title={`${d.label} · 시나리오 ${d.scenario_count}건`}
                >
                  <span>{d.label}</span>
                  <span
                    className={`mono text-[10px] rounded-full px-1.5 py-px ring-1 ${
                      active
                        ? "ring-[var(--hair)] text-[var(--ink-2)] bg-[var(--ink-tint,#f4f4f5)]"
                        : "ring-[var(--hair)] text-[var(--ink-3)] bg-white/50"
                    }`}
                  >
                    {d.scenario_count}
                  </span>
                </button>
              );
            })}
          </nav>
        </div>
        <div className="flex items-center gap-2">
          <div className="hidden md:flex items-center gap-2 px-2.5 py-1.5 rounded-lg ring-1 hair bg-white/80 text-[var(--ink-3)] text-[12px] min-w-[220px]">
            <Icon name="search" className="w-3.5 h-3.5" />
            <span>시나리오 / 서비스 검색</span>
            <kbd className="ml-auto mono text-[10px] ring-1 hair rounded px-1 py-px text-[var(--ink-3)]">
              ⌘K
            </kbd>
          </div>
          <div
            className="inline-flex items-center gap-2 px-2.5 py-1.5 rounded-lg ring-1 hair bg-white/80"
            title={backendOk ? "Backend healthy" : "Backend unreachable"}
          >
            <span
              className={`w-1.5 h-1.5 rounded-full ${
                backendOk ? "bg-emerald-500 pulse-ok" : "bg-rose-500"
              }`}
            />
            <span className="text-[12px] text-[var(--ink)]">
              {backendOk ? "Backend OK" : "Backend Down"}
            </span>
            <span className="mono text-[10.5px] text-[var(--ink-3)] border-l hair pl-2 ml-1">
              {envTag}
            </span>
          </div>
          <button className="w-8 h-8 rounded-lg ring-1 hair bg-white/80 hover:bg-white flex items-center justify-center text-[var(--ink-2)] relative">
            <Icon name="bell" className="w-4 h-4" />
            <span className="absolute top-1.5 right-1.5 w-1.5 h-1.5 rounded-full bg-rose-500 ring-2 ring-white" />
          </button>
        </div>
      </div>
    </header>
  );
}
