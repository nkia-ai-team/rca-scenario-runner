import { useEffect, useState } from "react";

import { api } from "../api/client";
import { presentationFor } from "../lib/tones";
import type { ApiScenario, ScenarioView } from "../types";

function toView(s: ApiScenario): ScenarioView {
  // presentationFor is keyed by within-domain short_id ("01", "02", ...)
  // so the legacy tone/tag map keeps working across all domains.
  const p = presentationFor(s.short_id);
  const hops = s.propagation
    .split(/\s*(?:→|->|>)\s*/)
    .map((x) => x.trim())
    .filter(Boolean);
  return { ...s, num: p.num, tag: p.tag, tone: p.tone, propagationHops: hops };
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
