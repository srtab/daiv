from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils.translation import gettext_lazy as _

import httpx

from core.site_settings import site_settings
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


def _site_rocketchat_credentials() -> tuple[str, str, str] | None:
    """Return (url, user_id, token) or None when any of the three is unset."""
    url = site_settings.rocketchat_url
    user_id = site_settings.rocketchat_user_id
    token = site_settings.rocketchat_auth_token
    if not url or not user_id or not token:
        return None
    return url, user_id, token.get_secret_value()


def verify_username(username: str) -> tuple[str | None, str | None]:
    """Look up a Rocket Chat user by username.

    Returns ``(rc_user_id, None)`` on success or ``(None, error_message)`` on failure.
    """
    creds = _site_rocketchat_credentials()
    if creds is None:
        return None, "Rocket Chat is not configured."
    url, user_id, token = creds
    endpoint = f"{url.rstrip('/')}/api/v1/users.info"
    headers = {"X-Auth-Token": token, "X-User-Id": user_id}
    try:
        response = httpx.get(endpoint, headers=headers, params={"username": username}, timeout=_RC_TIMEOUT_SECONDS)
    except httpx.RequestError as exc:
        return None, str(exc)
    if response.status_code >= 400:
        try:
            body = response.json()
            return None, body.get("error") or body.get("errorType") or f"HTTP {response.status_code}"
        except ValueError:
            return None, f"HTTP {response.status_code}"
    try:
        body = response.json()
    except ValueError:
        return None, "Invalid response from Rocket Chat."
    if body.get("success") is True and body.get("user", {}).get("_id"):
        return body["user"]["_id"], None
    return None, body.get("error") or body.get("errorType") or "User not found."


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
