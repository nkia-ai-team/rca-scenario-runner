import { useEffect, useState } from "react";

import { api } from "../api/client";

const POLL_INTERVAL_MS = 10_000;

export function useHealth(): boolean {
  const [ok, setOk] = useState(true);

  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null;
    let alive = true;

    const check = async () => {
      try {
        await api.healthz();
        if (alive) setOk(true);
      } catch {
        if (alive) setOk(false);
      }
    };

    check();
    timer = setInterval(check, POLL_INTERVAL_MS);

    return () => {
      alive = false;
      if (timer) clearInterval(timer);
    };
  }, []);

  return ok;
}
