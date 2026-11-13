from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import cache
from typing import TYPE_CHECKING, cast

from django.utils.translation import gettext_lazy as _

import httpx

from core.site_settings import site_settings
from notifications.channels.base import NotificationChannel
from notifications.channels.registry import register_channel
from notifications.channels.renderers.base import compose_plain_text
from notifications.channels.renderers.discord import DISCORD_MAX_CONTENT_CHARS, render
from notifications.choices import ChannelType
from notifications.exceptions import UnrecoverableDeliveryError

if TYPE_CHECKING:
    from notifications.models import Notification, NotificationDelivery

logger = logging.getLogger("daiv.notifications")

_DISCORD_API_BASE = "https://discord.com/api/v10"
_DISCORD_TIMEOUT_SECONDS = 5.0
_CANNOT_MESSAGE_USER = 50007


@cache
def _http() -> httpx.Client:
    """One pooled client per process; the bot token stays on each request, never on the client."""
    return httpx.Client(base_url=_DISCORD_API_BASE, timeout=_DISCORD_TIMEOUT_SECONDS)


class DiscordPermanentError(Exception):
    """Raised when Discord returns an error that retrying cannot resolve."""


class DiscordTransientError(Exception):
    """Raised for transport failures, rate limits, server errors, and invalid success responses."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class _DiscordClient:
    """Discord bot credential and authenticated API calls."""

    token: str = field(repr=False)

    @classmethod
    def from_site_settings(cls) -> _DiscordClient | None:
        token = site_settings.discord_bot_token
        if not token:
            return None
        return cls(token=token.get_secret_value())

    def request(self, method: str, path: str, payload: dict | None = None) -> httpx.Response:
        try:
            return _http().request(method, path, headers={"Authorization": f"Bot {self.token}"}, json=payload)
        except httpx.HTTPError as exc:
            # httpx frames hold the Authorization header in their locals; do not chain them into logs or Sentry.
            raise DiscordTransientError(f"Discord {method} {path} transport failure: {type(exc).__name__}") from None

    def call(self, method: str, path: str, payload: dict | None = None) -> dict:
        """Make an API call and classify permanent and retryable failures."""
        response = self.request(method, path, payload)
        status = response.status_code
        body = _json_object(response)
        error = _extract_error(body, status)

        if body is not None and body.get("code") == _CANNOT_MESSAGE_USER:
            logger.warning("Discord %s %s cannot message this user: %s", method, path, error)
            raise DiscordPermanentError(error)
        if status == 429:
            retry_after = _extract_retry_after(body)
            logger.warning("Discord %s %s returned HTTP 429 (retryable): %s", method, path, error)
            raise DiscordTransientError(f"Discord rate limited: {error}", retry_after)
        if status >= 500:
            logger.warning("Discord %s %s returned HTTP %s (retryable): %s", method, path, status, error)
            raise DiscordTransientError(f"Discord server error: {error}")
        if status >= 400:
            logger.warning("Discord %s %s returned HTTP %s: %s", method, path, status, error)
            raise DiscordPermanentError(error)
        if body is None:
            logger.warning("Discord %s %s returned a non-object JSON body with HTTP %s", method, path, status)
            raise DiscordTransientError(f"Discord returned an invalid response with HTTP {status}")
        return body

    def send_direct_message(self, user_id: str, payload: dict) -> None:
        dm = self.call("POST", "/users/@me/channels", {"recipient_id": user_id})
        channel_id = dm.get("id")
        if not isinstance(channel_id, str) or not channel_id:
            raise DiscordTransientError("Discord create-DM response did not include a channel ID")
        self.call("POST", f"/channels/{channel_id}/messages", payload)


def _json_object(response: httpx.Response) -> dict | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _extract_error(body: dict | None, status: int) -> str:
    if body is not None:
        message = body.get("message")
        if message:
            return str(message)
        if body.get("code") is not None:
            return f"Discord error code {body['code']}"
    return f"HTTP {status}"


def _extract_retry_after(body: dict | None) -> float | None:
    """Discord's top-level rate-limit delay, in seconds, when it is a positive number."""
    value = body.get("retry_after") if body is not None else None
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return float(value)


_MSG_NOT_CONFIGURED = _("Discord is not configured.")
_MSG_UNAVAILABLE = _("Discord is temporarily unavailable. Please try again.")
_MSG_INVALID_RESPONSE = _("Discord returned an invalid response.")
_MSG_USER_NOT_FOUND = _("Discord user not found.")


def verify_user_id(user_id: str) -> tuple[str | None, str | None]:
    """Return the verified Discord user ID, or a safe user-facing error message."""
    client = _DiscordClient.from_site_settings()
    if client is None:
        return None, str(_MSG_NOT_CONFIGURED)
    try:
        response = client.request("GET", f"/users/{user_id}")
    except DiscordTransientError as exc:
        logger.warning("Discord user verification transport error for %r: %s", user_id, exc)
        return None, str(_MSG_UNAVAILABLE)
    if response.status_code == 404:
        return None, str(_MSG_USER_NOT_FOUND)
    if response.status_code >= 400:
        logger.warning("Discord user verification returned HTTP %s for %r", response.status_code, user_id)
        return None, str(_MSG_UNAVAILABLE)
    body = _json_object(response)
    if body is None:
        logger.warning("Discord user verification returned a non-object JSON body for %r", user_id)
        return None, str(_MSG_INVALID_RESPONSE)
    if body.get("id") == user_id:
        return user_id, None
    logger.warning("Discord user verification returned a mismatched user ID for %r", user_id)
    return None, str(_MSG_INVALID_RESPONSE)


def _build_payload(notification: Notification) -> dict:
    """Build a create-message payload, falling back to plain text for unknown event types."""
    embed = render(notification)
    if embed is not None:
        return {"embeds": [embed]}
    logger.warning("Discord: no renderer for event_type=%r; sending plain text", notification.event_type)
    text = compose_plain_text(notification)
    if len(text) > DISCORD_MAX_CONTENT_CHARS:
        text = text[: DISCORD_MAX_CONTENT_CHARS - 1] + "…"
    return {"content": text}


@register_channel
class DiscordChannel(NotificationChannel):
    channel_type = ChannelType.DISCORD
    display_name = cast("str", _("Discord"))

    @classmethod
    def is_enabled(cls) -> bool:
        return bool(site_settings.discord_enabled)

    def send(self, notification: Notification, delivery: NotificationDelivery) -> None:
        if not self.is_enabled():
            raise UnrecoverableDeliveryError("Discord is disabled")
        client = _DiscordClient.from_site_settings()
        if client is None:
            raise UnrecoverableDeliveryError("Discord not configured")

        try:
            client.send_direct_message(delivery.address, _build_payload(notification))
        except DiscordPermanentError as exc:
            logger.error("Discord delivery %s permanently failed: %s", delivery.id, exc)
            raise UnrecoverableDeliveryError(str(exc)) from exc
