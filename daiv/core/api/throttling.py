from ninja.throttling import AuthRateThrottle

from core.site_settings import site_settings


class JobsRateThrottle(AuthRateThrottle):
    """Per-user throttle for endpoints that kick off agent runs.

    Rate is read from ``site_settings.jobs_throttle_rate`` at call time so the
    admin can change it without a redeploy. Both the API job endpoint and the
    chat completion endpoint use this — both spin up sandbox/agent work on
    each call, so a single per-user budget is the right default.
    """

    THROTTLE_RATES: dict[str, str | None] = {}

    def get_rate(self) -> str | None:
        return site_settings.jobs_throttle_rate


class ChatStreamRateThrottle(AuthRateThrottle):
    """Per-user rate throttle for ``GET /chat/stream``.

    The SSE stream holds an ASGI connection for up to the 300s duration cap and pins a
    pooled Redis connection through 15s blocking ``xread``\u00a0s, so an unbounded open
    rate lets one caller exhaust worker threads and the async Redis pool. This bounds the
    *open frequency* per user — combined with the async client's ``max_connections`` (the
    hard pool cap in :mod:`core.redis`) it keeps a single user from monopolising the bus.

    The rate is intentionally generous for legitimate reconnects: a long run resyncs once
    per 300s cap per tab, so even several concurrent tabs stay well under it, while a burst
    abuser is gated to ``RATE`` opens per minute before a 429 + ``Retry-After``.
    """

    # ``AuthRateThrottle`` keys the cache bucket on the authenticated user, so the budget
    # is per-user (not per-IP). Overridable in tests by patching the instance's
    # ``num_requests`` / ``duration`` (``SimpleRateThrottle`` reads those live).
    RATE = "30/min"

    THROTTLE_RATES: dict[str, str | None] = {}

    def get_rate(self) -> str | None:
        return self.RATE
