import { useEffect, useState } from "react";

export function useRunStatus(endpoint: string, opts: { intervalMs?: number } = {}) {
  const [active, setActive] = useState(false);
  const [stopped, setStopped] = useState(false);
  useEffect(() => {
    if (stopped) return;
    const ctrl = new AbortController();
    let cancelled = false;
    let id: ReturnType<typeof setInterval> | undefined;
    const stop = () => {
      cancelled = true;
      ctrl.abort();
      if (id) clearInterval(id);
      setStopped(true);
    };
    const poll = async () => {
      try {
        const r = await fetch(endpoint, { credentials: "include", signal: ctrl.signal });
        if (r.status === 401 || r.status === 403 || r.status === 404) {
          console.error(`useRunStatus: ${endpoint} returned ${r.status}; stopping poll`);
          stop();
          return;
        }
        if (!r.ok) {
          console.error(`useRunStatus: ${endpoint} returned ${r.status}`);
          return;
        }
        const { active: a } = (await r.json()) as { active: boolean };
        if (!cancelled) setActive(Boolean(a));
      } catch (err) {
        if ((err as { name?: string }).name === "AbortError") return;
        console.error("useRunStatus: poll failed", err);
      }
    };
    poll();
    id = setInterval(poll, opts.intervalMs ?? 5000);
    return () => {
      cancelled = true;
      ctrl.abort();
      clearInterval(id);
    };
  }, [endpoint, opts.intervalMs, stopped]);
  return { active };
}
