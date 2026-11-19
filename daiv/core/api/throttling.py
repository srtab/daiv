from typing import TYPE_CHECKING

from ninja.throttling import AuthRateThrottle

from core.site_settings import site_settings

if TYPE_CHECKING:
    from django.http import HttpRequest


class PerUserRateThrottle(AuthRateThrottle):
    """Buckets keyed on the user's primary key, not ninja's default ``str(request.auth)``, which for ``User`` is the
    display name: shared by users and editable by its owner."""

    def get_cache_key(self, request: HttpRequest) -> str:
        user = getattr(request, "auth", None)
        if user is None:
            return super().get_cache_key(request)
        return self.cache_format % {"scope": self.scope, "ident": user.pk}


class JobsRateThrottle(PerUserRateThrottle):
    """Per-user throttle for endpoints that kick off agent runs.

    Rate is read from ``site_settings.jobs_throttle_rate`` at call time so the
    admin can change it without a redeploy. Both the API job endpoint and the
    chat completion endpoint use this — both spin up sandbox/agent work on
    each call, so a single per-user budget is the right default.
    """

    THROTTLE_RATES: dict[str, str | None] = {}

    def get_rate(self) -> str | None:
        return site_settings.jobs_throttle_rate


class SpendRateThrottle(PerUserRateThrottle):
    """Per-user throttle for reading spend records. A read starts no agent work, so the fixed rate only guards
    against runaway clients, in a bucket of its own."""

    scope = "spend"
    RATE = "60/m"

    def __init__(self, rate: str = RATE) -> None:
        super().__init__(rate)
