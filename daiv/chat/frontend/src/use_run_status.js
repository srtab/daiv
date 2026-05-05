import { useEffect, useState } from "react";
export function useRunStatus(endpoint, opts = {}) {
    const [active, setActive] = useState(false);
    useEffect(() => {
        let cancelled = false;
        const poll = async () => {
            try {
                const r = await fetch(endpoint, { credentials: "include" });
                if (!r.ok)
                    return;
                const { active: a } = (await r.json());
                if (!cancelled)
                    setActive(Boolean(a));
            }
            catch {
                // best-effort poll
            }
        };
        poll();
        const id = setInterval(poll, opts.intervalMs ?? 5000);
        return () => {
            cancelled = true;
            clearInterval(id);
        };
    }, [endpoint, opts.intervalMs]);
    return { active };
}
