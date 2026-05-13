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
