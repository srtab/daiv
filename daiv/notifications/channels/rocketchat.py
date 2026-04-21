from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils.translation import gettext_lazy as _

import httpx

from notifications.channels.base import NotificationChannel
from notifications.channels.registry import register_channel
from notifications.choices import ChannelType
from notifications.models import UserChannelBinding

if TYPE_CHECKING:
    from accounts.models import User
    from notifications.models import Notification, NotificationDelivery

logger = logging.getLogger("daiv.notifications")


class RocketChatPermanentError(Exception):
    """Raised when Rocket Chat returns a permanent error (4xx or a known non-retryable error code)."""


_PERMANENT_ERROR_TYPES = frozenset({
    "error-invalid-channel",
    "error-invalid-room",
    "error-user-not-found",
    "error-not-allowed",
})

_RC_TIMEOUT_SECONDS = 5.0


def _rc_post(url: str, user_id: str, token: str, method: str, payload: dict) -> dict:
    """POST to Rocket Chat's REST API.

    Raises ``RocketChatPermanentError`` for permanent failures (4xx, or 2xx with a
    known permanent error code). Other failures propagate so the caller's retry
    loop engages.
    """
    endpoint = f"{url.rstrip('/')}/api/v1/{method}"
    headers = {"X-Auth-Token": token, "X-User-Id": user_id, "Content-Type": "application/json"}
    response = httpx.post(endpoint, headers=headers, json=payload, timeout=_RC_TIMEOUT_SECONDS)
    if 400 <= response.status_code < 500:
        try:
            body = response.json()
        except ValueError:
            body = {}
        error = body.get("error") or body.get("errorType") or f"HTTP {response.status_code}"
        raise RocketChatPermanentError(str(error))
    response.raise_for_status()
    body = response.json()
    if body.get("success") is True:
        return body
    if body.get("errorType") in _PERMANENT_ERROR_TYPES:
        raise RocketChatPermanentError(str(body.get("error") or body.get("errorType")))
    raise RuntimeError(f"Rocket Chat call to {method} failed: {body!r}")


@register_channel
class RocketChatChannel(NotificationChannel):
    channel_type = ChannelType.ROCKETCHAT
    display_name = _("Rocket Chat")

    def resolve_address(self, user: User) -> str | None:
        binding = (
            UserChannelBinding.objects
            .filter(user=user, channel_type=self.channel_type, is_verified=True)
            .order_by("-modified")
            .first()
        )
        return binding.address if binding else None

    def send(self, notification: Notification, delivery: NotificationDelivery) -> None:
        raise NotImplementedError("send() implemented in a later task")
