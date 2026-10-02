import { useEffect, useState } from "react";

import { api } from "../api/client";
import { presentationFor } from "../lib/tones";
import type { ApiScenario, ScenarioView } from "../types";

function toView(s: ApiScenario): ScenarioView {
  // presentationFor is keyed by within-domain short_id ("01", "02", ...)
  // so the legacy tone/tag map keeps working across all domains.
  const p = presentationFor(s.short_id);
  // 현행 시나리오는 서버가 단계 리스트를 준다. 문장 안의 ">"(예: "p95 > 2s")를
  // 화살표로 오인해 쪼개지 않도록 그쪽을 우선한다.
  const hops =
    s.propagation_steps ??
    s.propagation
      .split(/\s*(?:→|->|>)\s*/)
      .map((x) => x.trim())
      .filter(Boolean);
  return {
    ...s,
    num: p.num,
    code: p.code,
    // 현행 시나리오의 태그는 관측 도메인("DPM, KCM") — 어디서 보일지가 곧 분류다.
    tag: s.cause_domain ?? p.tag,
    tone: p.tone,
    propagationHops: hops,
  };
}

export function useScenarios() {
  const [scenarios, setScenarios] = useState<ScenarioView[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .listScenarios()
      .then((list) => {
        if (!alive) return;
        setScenarios(list.map(toView));
        setError(null);
      })
      .catch((e: Error) => {
        if (!alive) return;
        setError(e.message);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  return { scenarios, loading, error };
}
