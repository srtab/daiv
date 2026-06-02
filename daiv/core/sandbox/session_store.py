from __future__ import annotations

import logging

from django.core.cache import cache

logger = logging.getLogger("daiv.sandbox")

# Warm sandbox sessions are remembered per conversation thread for this long. Ideally kept ≤ the
# sandbox's DAIV_SANDBOX_SESSION_GRACE_SECONDS (configured in the separate daiv-sandbox service, so
# not enforceable here) so the mapping expires around when the reaper removes the stopped container.
# A mismatch is only a perf miss, not a bug: a reaped session fails the caller's liveness check,
# which drops the stale mapping (via :meth:`SandboxSessionStore.forget`) and falls back to a cold
# create+seed.
SANDBOX_SESSION_TTL_SECONDS = 12 * 60 * 60


class SandboxSessionStore:
    """Best-effort persistence of the conversation thread → sandbox session-id mapping.

    Warm-session reuse is a pure optimization, so every operation degrades to a no-op / "no warm
    session" on a cache outage rather than failing the agent run.

    This object is *pure persistence*: it has no knowledge of the sandbox client or of session
    liveness. The caller is responsible for validating that a returned session still exists (and
    calling :meth:`forget` when it does not). Keeping it free of sandbox-transport concerns is what
    lets the backing store be swapped (cache → checkpointer state → redis) without touching the
    middleware that orchestrates reuse.
    """

    def __init__(self, *, ttl: int = SANDBOX_SESSION_TTL_SECONDS) -> None:
        self._ttl = ttl

    @staticmethod
    def _key(thread_id: str) -> str:
        return f"sandbox_session:{thread_id}"

    async def aget(self, thread_id: str) -> str | None:
        """Return the remembered session id for *thread_id*, or None.

        None covers all the "no warm session" cases: no mapping, a malformed mapping, or a cache
        read failure — the caller then falls back to a cold create+seed.
        """
        try:
            cached = await cache.aget(self._key(thread_id))
        except Exception:
            logger.exception("Sandbox-session cache read failed for %s; falling back to cold create", thread_id)
            return None
        return cached.get("session_id") if isinstance(cached, dict) else None

    async def remember(self, thread_id: str, session_id: str) -> None:
        """Persist (and refresh the TTL of) the thread → session mapping.

        A failure only costs the next turn a cold create+seed; it must never propagate into the
        run's teardown logic.
        """
        try:
            await cache.aset(self._key(thread_id), {"session_id": session_id}, timeout=self._ttl)
        except Exception:
            logger.exception("Sandbox-session cache write failed for %s; reuse may not persist", thread_id)

    async def forget(self, thread_id: str) -> None:
        """Drop a stale mapping (best-effort; see :meth:`aget`)."""
        try:
            await cache.adelete(self._key(thread_id))
        except Exception:
            logger.exception("Sandbox-session cache delete failed for %s", thread_id)
