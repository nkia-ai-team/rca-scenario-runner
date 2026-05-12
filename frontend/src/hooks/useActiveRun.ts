import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { ActiveRun } from "../types";

const POLL_INTERVAL_MS = 3_000;

const EMPTY: ActiveRun = {
  is_active: false,
  scenario_id: null,
  run_id: null,
  mode: null,
  started_at: null,
};

/**
 * Global occupancy poll — tells every browser tab whether *someone* is
 * currently running a scenario on this backend. Used to disable the 실행
 * buttons across all domains so two users can't race against the backend's
 * asyncio.Lock and trade HTTP 409s.
 */
export function useActiveRun(): ActiveRun {
  const [active, setActive] = useState<ActiveRun>(EMPTY);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setInterval> | null = null;

    const tick = async () => {
      try {
        const next = await api.active();
        if (alive) setActive(next);
      } catch {
        if (alive) setActive(EMPTY);
      }
    };

    tick();
    timer = setInterval(tick, POLL_INTERVAL_MS);

    return () => {
      alive = false;
      if (timer) clearInterval(timer);
    };
  }, []);

  return active;
}
