from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.utils.translation import gettext_lazy as _

import httpx

from core.site_settings import site_settings
from core.utils import build_absolute_url, build_uri
from notifications.channels.base import NotificationChannel
from notifications.channels.registry import register_channel
from notifications.choices import ChannelType
from notifications.exceptions import UnrecoverableDeliveryError

if TYPE_CHECKING:
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


@dataclass(frozen=True)
class _RCClient:
    """Bundled Rocket Chat credentials + convenience HTTP calls.

    ``token`` is excluded from the default repr to avoid leaking the bot secret
    via log lines, exception tracebacks, or test output.
    """

    url: str
    user_id: str
    token: str = field(repr=False)

    @classmethod
    def from_site_settings(cls) -> _RCClient | None:
        url = site_settings.rocketchat_url
        user_id = site_settings.rocketchat_user_id
        token = site_settings.rocketchat_auth_token
        if not url or not user_id or not token:
            return None
        return cls(url=url, user_id=user_id, token=token.get_secret_value())

    @property
    def _headers(self) -> dict[str, str]:
        return {"X-Auth-Token": self.token, "X-User-Id": self.user_id}

    def post(self, method: str, payload: dict) -> httpx.Response:
        return httpx.post(
            build_uri(self.url, f"api/v1/{method}"),
            headers={**self._headers, "Content-Type": "application/json"},
            json=payload,
            timeout=_RC_TIMEOUT_SECONDS,
        )

    def get(self, method: str, params: dict) -> httpx.Response:
        return httpx.get(
            build_uri(self.url, f"api/v1/{method}"), headers=self._headers, params=params, timeout=_RC_TIMEOUT_SECONDS
        )


def _extract_rc_error(response: httpx.Response, default: str | None = None) -> str:
    """Best-effort error string from an RC response body, falling back to HTTP status."""
    try:
        body = response.json()
    except ValueError:
        body = {}
    return body.get("error") or body.get("errorType") or default or f"HTTP {response.status_code}"


def _rc_post(client: _RCClient, method: str, payload: dict) -> dict:
    """POST to Rocket Chat's REST API.

    Raises ``RocketChatPermanentError`` for permanent failures (4xx, or 2xx with a
    known permanent error code). Other failures propagate so the caller's retry
    loop engages.
    """
    response = client.post(method, payload)
    if 400 <= response.status_code < 500:
        error = _extract_rc_error(response)
        logger.warning("Rocket Chat %s returned HTTP %s: %s", method, response.status_code, error)
        raise RocketChatPermanentError(error)
    response.raise_for_status()
    body = response.json()
    if body.get("success") is True:
        return body
    error_type = body.get("errorType")
    if error_type in _PERMANENT_ERROR_TYPES:
        error = str(body.get("error") or error_type)
        logger.warning("Rocket Chat %s permanent error %r: %s", method, error_type, error)
        raise RocketChatPermanentError(error)
    logger.warning(
        "Rocket Chat %s unexpected response (retryable): status=%s errorType=%r",
        method,
        response.status_code,
        error_type,
    )
    raise RuntimeError(f"Rocket Chat call to {method} failed with unknown errorType={error_type!r}")


# Generic user-facing strings — raw transport errors can leak internal hostnames,
# so we log the detail server-side and show a neutral message.
_MSG_NOT_CONFIGURED = _("Rocket Chat is not configured.")
_MSG_UNAVAILABLE = _("Rocket Chat is temporarily unavailable. Please try again.")
_MSG_INVALID_RESPONSE = _("Rocket Chat returned an invalid response.")
_MSG_USER_NOT_FOUND = _("Rocket Chat user not found.")


def verify_username(username: str) -> tuple[str | None, str | None]:
    """Look up a Rocket Chat user by username.

    Returns ``(rc_user_id, None)`` on success or ``(None, user_facing_message)`` on
    failure. The message is safe to flash directly to the end user; detailed errors
    are logged server-side.
    """
    client = _RCClient.from_site_settings()
    if client is None:
        return None, str(_MSG_NOT_CONFIGURED)
    try:
        response = client.get("users.info", {"username": username})
    except httpx.RequestError as exc:
        logger.warning("Rocket Chat users.info transport error for %r: %s", username, exc)
        return None, str(_MSG_UNAVAILABLE)
    if response.status_code >= 400:
        logger.warning(
            "Rocket Chat users.info HTTP %s for %r: %s", response.status_code, username, _extract_rc_error(response)
        )
        return None, str(_MSG_USER_NOT_FOUND if response.status_code < 500 else _MSG_UNAVAILABLE)
    try:
        body = response.json()
    except ValueError:
        logger.warning("Rocket Chat users.info returned non-JSON body for %r", username)
        return None, str(_MSG_INVALID_RESPONSE)
    if body.get("success") is True and body.get("user", {}).get("_id"):
        return body["user"]["_id"], None
    logger.info("Rocket Chat users.info did not resolve %r: %s", username, _extract_rc_error(response))
    return None, str(_MSG_USER_NOT_FOUND)


def _compose_text(notification: Notification) -> str:
    parts = [notification.subject, "", notification.body]
    if notification.link_url:
        parts.extend(["", build_absolute_url(notification.link_url)])
    return "\n".join(parts)


@register_channel
class RocketChatChannel(NotificationChannel):
    channel_type = ChannelType.ROCKETCHAT
    display_name = _("Rocket Chat")

    def send(self, notification: Notification, delivery: NotificationDelivery) -> None:
        client = _RCClient.from_site_settings()
        if client is None:
            raise UnrecoverableDeliveryError("Rocket Chat not configured")

        text = _compose_text(notification)
        try:
            _rc_post(client, "chat.postMessage", {"channel": f"@{delivery.address}", "text": text})
        except RocketChatPermanentError as exc:
            logger.error("Rocket Chat delivery %s permanently failed: %s", delivery.id, exc)
            raise UnrecoverableDeliveryError(str(exc)) from exc
