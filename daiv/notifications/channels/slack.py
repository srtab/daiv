from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.utils.translation import gettext_lazy as _

import httpx

from core.site_settings import site_settings
from notifications.channels.base import NotificationChannel
from notifications.channels.registry import register_channel
from notifications.channels.renderers.attachments import render
from notifications.channels.renderers.base import compose_plain_text
from notifications.choices import ChannelType
from notifications.exceptions import UnrecoverableDeliveryError

if TYPE_CHECKING:
    from notifications.models import Notification, NotificationDelivery

logger = logging.getLogger("daiv.notifications")


class SlackError(Exception):
    """Any Slack Web API failure."""


class SlackPermanentError(SlackError):
    """Raised when Slack returns a permanent error (one of the known non-retryable error codes)."""


class SlackTransientError(SlackError):
    """Raised for 429, 5xx, a ``ratelimited`` body, or a 2xx body that is not a Slack envelope.

    Never *under* ``SlackPermanentError``: the caller's retry ladder has to keep engaging.
    ``retry_after`` carries Slack's own ``Retry-After`` wait in seconds when it named one,
    which the delivery ladder honours as a floor.
    """

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


# Slack answers most API errors with HTTP 200 and ``{"ok": false, "error": "..."}``.
# These codes name a problem no retry can fix: the member is gone, the bot token
# itself is rejected, the app cannot DM, or the request itself is malformed.
# Anything else is treated as retryable.
_PERMANENT_ERRORS = frozenset({
    "user_not_found",
    "channel_not_found",
    "invalid_auth",
    "account_inactive",
    "not_authed",
    "token_revoked",
    "token_expired",
    "missing_scope",
    "not_allowed_token_type",
    "messages_tab_disabled",
    "team_access_not_granted",
    "access_denied",
    "no_permission",
    "invalid_arguments",
    "missing_argument",
    "too_many_attachments",
})

# Permanent errors that mean the bot token is broken rather than the member ID:
# during verification the admin-facing "not configured" message is the honest one.
_TOKEN_ERRORS = frozenset({
    "invalid_auth",
    "account_inactive",
    "not_authed",
    "token_revoked",
    "token_expired",
    "missing_scope",
    "not_allowed_token_type",
})

_SLACK_API_BASE = "https://slack.com/api"
_SLACK_TIMEOUT_SECONDS = 5.0


def _extract_retry_after(response: httpx.Response) -> int | None:
    """Slack's own rate-limit wait in seconds, when it named a usable one."""
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        seconds = int(value)
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return seconds


def _envelope(response: httpx.Response) -> dict | None:
    """The body as a Slack envelope, or ``None`` when it is not a JSON object."""
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


@dataclass(frozen=True)
class _SlackClient:
    """The Slack bot token + convenience Web API calls.

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

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def post(self, method: str, payload: dict) -> httpx.Response:
        return httpx.post(
            f"{_SLACK_API_BASE}/{method}",
            headers={**self._headers, "Content-Type": "application/json; charset=utf-8"},
            json=payload,
            timeout=_SLACK_TIMEOUT_SECONDS,
        )

    def get(self, method: str, params: dict) -> httpx.Response:
        # ``users.info`` ignores JSON bodies, so its argument rides as a query parameter.
        return httpx.get(
            f"{_SLACK_API_BASE}/{method}", headers=self._headers, params=params, timeout=_SLACK_TIMEOUT_SECONDS
        )

    def call(self, method: str, payload: dict) -> dict:
        """POST to Slack's Web API and classify the outcome.

        Raises ``SlackPermanentError`` for the permanent ``ok: false`` error codes and
        for other 4xx statuses. 429, 5xx, a ``ratelimited`` body, and non-envelope
        bodies raise ``SlackTransientError`` so the caller's retry loop engages.
        """
        response = self.post(method, payload)
        status = response.status_code
        if status == 429:
            retry_after = _extract_retry_after(response)
            logger.warning("Slack %s rate limited (HTTP 429); retry_after=%s", method, retry_after)
            raise SlackTransientError(f"Slack {method} rate limited (HTTP 429)", retry_after)
        if status >= 500:
            logger.warning("Slack %s returned HTTP %s (retryable)", method, status)
            raise SlackTransientError(f"Slack {method} failed with HTTP {status}")
        if status >= 400:
            error = _extract_slack_error(response, status)
            logger.warning("Slack %s returned HTTP %s: %s", method, status, error)
            raise SlackPermanentError(error)
        body = _envelope(response)
        if body is None:
            # A 2xx that is not a Slack envelope means something interposed (a proxy, a
            # captive portal); the next attempt may well reach Slack, so this is transient.
            logger.warning("Slack %s returned a non-envelope body with HTTP %s", method, status)
            raise SlackTransientError(f"Slack {method} returned a non-envelope body with HTTP {status}")
        if body.get("ok") is True:
            return body
        error = body.get("error")
        if error in _PERMANENT_ERRORS:
            logger.warning("Slack %s permanent error: %s", method, error)
            raise SlackPermanentError(str(error))
        if error == "ratelimited":
            retry_after = _extract_retry_after(response)
            logger.warning("Slack %s rate limited; retry_after=%s", method, retry_after)
            raise SlackTransientError(f"Slack {method} rate limited", retry_after)
        logger.warning("Slack %s unexpected response (retryable): error=%r", method, error)
        raise SlackTransientError(f"Slack call to {method} failed with error={error!r}")

    def verify_member_id(self, member_id: str) -> tuple[str | None, str | None]:
        """Look up a Slack member by ID via ``users.info``.

        Returns ``(member_id, None)`` on success or ``(None, user_facing_message)`` on
        failure. The message is safe to flash directly to the end user; detailed errors
        are logged server-side.
        """
        try:
            response = self.get("users.info", {"user": member_id})
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
        if body.get("ok") is True:
            user = body.get("user") or {}
            if user.get("id"):
                if user.get("deleted"):
                    return None, str(_MSG_MEMBER_DEACTIVATED)
                if user.get("is_bot"):
                    return None, str(_MSG_MEMBER_IS_BOT)
                if user.get("is_stranger"):
                    return None, str(_MSG_MEMBER_IS_STRANGER)
                return user["id"], None
        error = body.get("error")
        if error in _TOKEN_ERRORS:
            # Per constitutions/patterns/errors.md rule 1, an operator-actionable failure
            # logs at ERROR, because Sentry ignores warnings.
            logger.error("Slack users.info rejected the bot token for %r: %s", member_id, error)
            return None, str(_MSG_NOT_CONFIGURED)
        logger.info("Slack users.info did not resolve %r: %s", member_id, error)
        return None, str(_MSG_USER_NOT_FOUND)

    @staticmethod
    def build_payload(notification: Notification, delivery: NotificationDelivery) -> dict:
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


def _extract_slack_error(response: httpx.Response, status: int) -> str:
    """Best-effort error string from a Slack response body, falling back to the HTTP status."""
    body = _envelope(response)
    error = body.get("error") if body is not None else None
    return str(error) if error else f"HTTP {status}"


# Generic user-facing strings — raw transport errors can leak internal detail,
# so we log the detail server-side and show a neutral message.
_MSG_NOT_CONFIGURED = _("Slack is not configured.")
_MSG_UNAVAILABLE = _("Slack is temporarily unavailable. Please try again.")
_MSG_INVALID_RESPONSE = _("Slack returned an invalid response.")
_MSG_USER_NOT_FOUND = _("Slack user not found.")
_MSG_MEMBER_DEACTIVATED = _("That Slack account is deactivated.")
_MSG_MEMBER_IS_BOT = _("Slack bot accounts can't receive notifications.")
_MSG_MEMBER_IS_STRANGER = _("That Slack member belongs to another organisation.")


def verify_member_id(member_id: str) -> tuple[str | None, str | None]:
    """Look up a Slack member by ID via ``users.info``.

    Returns ``(member_id, None)`` on success or ``(None, user_facing_message)`` on
    failure. The message is safe to flash directly to the end user; detailed errors
    are logged server-side.
    """
    client = _SlackClient.from_site_settings()
    if client is None:
        return None, str(_MSG_NOT_CONFIGURED)
    return client.verify_member_id(member_id)


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
            client.call("chat.postMessage", client.build_payload(notification, delivery))
        except SlackPermanentError as exc:
            logger.error("Slack delivery %s permanently failed: %s", delivery.id, exc)
            raise UnrecoverableDeliveryError(str(exc)) from exc
