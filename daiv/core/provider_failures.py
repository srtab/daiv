from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.utils import timezone
from django.utils.translation import gettext

from core.models import Provider, ProviderFailureKind

if TYPE_CHECKING:
    from datetime import datetime

ADMIN_KINDS = frozenset({
    ProviderFailureKind.OUT_OF_CREDITS,
    ProviderFailureKind.SPEND_LIMIT_REACHED,
    ProviderFailureKind.AUTH_FAILED,
})


def failure_message(kind: str, provider_slug: str) -> str:
    provider = Provider.display_name_for(provider_slug)
    match kind:
        case ProviderFailureKind.OUT_OF_CREDITS:
            template = gettext(
                "%(provider)s is out of credits, so DAIV couldn't finish this run. "
                "An admin needs to top up the %(provider)s account."
            )
        case ProviderFailureKind.SPEND_LIMIT_REACHED:
            template = gettext(
                "DAIV has reached the spending limit set on its %(provider)s key, so it couldn't finish this run. "
                "An admin needs to raise the limit or wait for it to reset."
            )
        case ProviderFailureKind.AUTH_FAILED:
            template = gettext(
                "%(provider)s rejected DAIV's API key, so DAIV couldn't finish this run. "
                "An admin needs to update the key in the provider settings."
            )
        case ProviderFailureKind.RATE_LIMITED:
            template = gettext("%(provider)s is rate-limiting DAIV right now. Try again in a few minutes.")
        case ProviderFailureKind.PROVIDER_UNAVAILABLE:
            template = gettext("%(provider)s is having problems right now. Try again in a few minutes.")
        case _:
            raise ValueError(f"Unknown provider failure kind: {kind!r}")
    return template % {"provider": provider}


@dataclass(frozen=True)
class ProviderFailure:
    kind: str
    provider_slug: str

    def message(self) -> str:
        return failure_message(self.kind, self.provider_slug)


def active_provider_failures(now: datetime | None = None) -> list[Provider.Cached]:
    now = now or timezone.now()
    return [row for row in Provider.get_cached_rows() if row.is_enabled and row.active_failure_kind(now) in ADMIN_KINDS]
