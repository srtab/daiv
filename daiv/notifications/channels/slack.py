from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.utils.translation import gettext_lazy as _

import httpx

from core.site_settings import site_settings
from notifications.channels.base import NotificationChannel
from notifications.channels.registry import register_channel
from notifications.channels.renderers.base import compose_plain_text
from notifications.channels.renderers.slack import render
from notifications.choices import ChannelType
from notifications.exceptions import UnrecoverableDeliveryError

if TYPE_CHECKING:
    from notifications.models import Notification, NotificationDelivery

logger = logging.getLogger("daiv.notifications")


class SlackPermanentError(Exception):
    """Raised when Slack returns a permanent error (one of the known non-retryable error codes)."""


# Slack answers most API errors with HTTP 200 and ``{"ok": false, "error": "..."}``.
# These codes name a problem no retry can fix: the member is gone, or the bot
# token itself is rejected. Anything else is treated as retryable.
_PERMANENT_ERRORS = frozenset({"user_not_found", "channel_not_found", "invalid_auth", "account_inactive", "not_authed"})

# Permanent errors that mean the bot token is broken rather than the member ID:
# during verification the admin-facing "not configured" message is the honest one.
_TOKEN_ERRORS = frozenset({"invalid_auth", "account_inactive", "not_authed"})

_SLACK_API_BASE = "https://slack.com/api"
_SLACK_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class _SlackClient:
    """The Slack bot token + convenience HTTP calls.

    ``token`` is excluded from the default repr to avoid leaking the bot secret
    via log lines, exception tracebacks, or test output.
    """

    token: str = field(repr=False)

    @classmethod
    def from_site_settings(cls) -> _SlackClient | None:
        token = site_settings.slack_bot_token
        if not token:
            return None
        return cls(token=token.get_secret_value())

    def post(self, method: str, payload: dict) -> httpx.Response:
        return httpx.post(
            f"{_SLACK_API_BASE}/{method}",
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json; charset=utf-8"},
            json=payload,
            timeout=_SLACK_TIMEOUT_SECONDS,
        )


def _slack_post(client: _SlackClient, method: str, payload: dict) -> dict:
    """POST to Slack's Web API.

    Raises ``SlackPermanentError`` for the permanent ``ok: false`` error codes.
    HTTP failures and any other ``ok: false`` error propagate so the caller's
    retry loop engages.
    """
    response = client.post(method, payload)
    response.raise_for_status()
    body = response.json()
    if body.get("ok") is True:
        return body
    error = body.get("error")
    if error in _PERMANENT_ERRORS:
        logger.warning("Slack %s permanent error: %s", method, error)
        raise SlackPermanentError(str(error))
    logger.warning("Slack %s unexpected response (retryable): error=%r", method, error)
    raise RuntimeError(f"Slack call to {method} failed with error={error!r}")


# Generic user-facing strings — raw transport errors can leak internal detail,
# so we log the detail server-side and show a neutral message.
_MSG_NOT_CONFIGURED = _("Slack is not configured.")
_MSG_UNAVAILABLE = _("Slack is temporarily unavailable. Please try again.")
_MSG_INVALID_RESPONSE = _("Slack returned an invalid response.")
_MSG_USER_NOT_FOUND = _("Slack user not found.")


def verify_member_id(member_id: str) -> tuple[str | None, str | None]:
    """Look up a Slack member by ID via ``users.info``.

    Returns ``(member_id, None)`` on success or ``(None, user_facing_message)`` on
    failure. The message is safe to flash directly to the end user; detailed errors
    are logged server-side.
    """
    client = _SlackClient.from_site_settings()
    if client is None:
        return None, str(_MSG_NOT_CONFIGURED)
    try:
        response = client.post("users.info", {"user": member_id})
    except httpx.RequestError as exc:
        logger.warning("Slack users.info transport error for %r: %s", member_id, exc)
        return None, str(_MSG_UNAVAILABLE)
    if response.status_code >= 400:
        logger.warning("Slack users.info HTTP %s for %r", response.status_code, member_id)
        unavailable = response.status_code == 429 or response.status_code >= 500
        return None, str(_MSG_UNAVAILABLE if unavailable else _MSG_USER_NOT_FOUND)
    try:
        body = response.json()
    except ValueError:
        logger.warning("Slack users.info returned non-JSON body for %r", member_id)
        return None, str(_MSG_INVALID_RESPONSE)
    if body.get("ok") is True and body.get("user", {}).get("id"):
        return body["user"]["id"], None
    error = body.get("error")
    if error in _TOKEN_ERRORS:
        logger.warning("Slack users.info rejected the bot token for %r: %s", member_id, error)
        return None, str(_MSG_NOT_CONFIGURED)
    logger.info("Slack users.info did not resolve %r: %s", member_id, error)
    return None, str(_MSG_USER_NOT_FOUND)


def _build_payload(notification: Notification, delivery: NotificationDelivery) -> dict:
    """Build the ``chat.postMessage`` body for ``notification``.

    Posting with the member ID as ``channel`` opens a direct message from the
    bot. Falls back to a plain-text message when the event type has no
    ``EVENT_ROWS`` definition, so newly-introduced events deliver before their
    rows ship.
    """
    rendered = render(notification)
    if rendered is None:
        # Log so a missing renderer doesn't silently degrade to plain text forever.
        logger.warning("Slack: no renderer for event_type=%r; sending plain text", notification.event_type)
        return {"channel": delivery.address, "text": compose_plain_text(notification)}
    text, attachments = rendered
    return {"channel": delivery.address, "text": text, "attachments": attachments}


@register_channel
class SlackChannel(NotificationChannel):
    channel_type = ChannelType.SLACK
    display_name = _("Slack")

    @classmethod
    def is_enabled(cls) -> bool:
        return bool(site_settings.slack_enabled)

    def send(self, notification: Notification, delivery: NotificationDelivery) -> None:
        if not self.is_enabled():
            raise UnrecoverableDeliveryError("Slack is disabled")

        client = _SlackClient.from_site_settings()
        if client is None:
            raise UnrecoverableDeliveryError("Slack not configured")

        try:
            _slack_post(client, "chat.postMessage", _build_payload(notification, delivery))
        except SlackPermanentError as exc:
            logger.error("Slack delivery %s permanently failed: %s", delivery.id, exc)
            raise UnrecoverableDeliveryError(str(exc)) from exc
