from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from core.site_settings import site_settings

logger = logging.getLogger("daiv.notifications")

_TG_API_BASE = "https://api.telegram.org"
_TG_TIMEOUT_SECONDS = 5.0

# Telegram exposes no machine code for a chat it can never deliver to again, so the flip in
# ``TelegramChannel.send`` matches on wording. Acknowledged fragile: if it drifts the flip stops
# firing and the delivery is what it would have been anyway — a permanent FAILED.
_TG_UNREACHABLE_DESCRIPTIONS = frozenset({"bot was blocked by the user", "user is deactivated", "chat not found"})

# The only update kinds the webhook acts on; narrowing stops Telegram delivering the rest.
ALLOWED_UPDATES = ["message", "my_chat_member"]


class TelegramError(Exception):
    """Any Bot API failure."""


class TelegramPermanentError(TelegramError):
    """Raised when Telegram returns a failure that will never succeed on retry."""


class TelegramTransientError(TelegramError):
    """Raised for 429, 5xx, or a 2xx body that is not a Bot API envelope.

    Never *under* ``TelegramPermanentError``: the caller's retry ladder has to keep engaging.
    """


class TelegramTransportError(TelegramTransientError):
    """Raised when the Bot API could not be reached, or answered something unreadable.

    Raised ``from None`` when it wraps an httpx failure — those frames hold the request URL in
    their locals, and the Bot API carries the token in that URL's path.
    """


def is_unreachable_chat_error(description: str) -> bool:
    """True when a permanent failure means the chat can never receive again."""
    lowered = description.lower()
    return any(fragment in lowered for fragment in _TG_UNREACHABLE_DESCRIPTIONS)


@dataclass(frozen=True)
class TGClient:
    """The bot token plus convenience Bot API calls.

    ``token`` is excluded from the default repr: it is a full-control credential and would
    otherwise reach log lines, tracebacks, and test output. The repr guard is only half of it —
    the Bot API also carries the token in the request *path*, which is why ``post`` re-raises
    ``from None`` (httpx frames hold the URL in their locals) and why ``settings/components/
    sentry.py`` redacts it from breadcrumbs and spans.
    """

    token: str = field(repr=False)

    @classmethod
    def from_site_settings(cls) -> TGClient | None:
        token = site_settings.telegram_bot_token
        if not token:
            return None
        return cls(token=token.get_secret_value())

    def post(self, method: str, payload: dict) -> httpx.Response:
        try:
            return httpx.post(f"{_TG_API_BASE}/bot{self.token}/{method}", json=payload, timeout=_TG_TIMEOUT_SECONDS)
        except httpx.HTTPError as exc:
            raise TelegramTransportError(f"Telegram {method} transport failure: {type(exc).__name__}") from None

    def send_message(self, payload: dict) -> dict:
        return _tg_post(self, "sendMessage", payload)

    def get_me(self) -> dict:
        return _tg_post(self, "getMe", {})

    def set_webhook(self, *, url: str, secret_token: str) -> dict:
        return _tg_post(
            self, "setWebhook", {"url": url, "secret_token": secret_token, "allowed_updates": ALLOWED_UPDATES}
        )

    def delete_webhook(self) -> dict:
        return _tg_post(self, "deleteWebhook", {})

    def get_webhook_info(self) -> dict:
        return _tg_post(self, "getWebhookInfo", {})


def _extract_tg_error(response: httpx.Response) -> str:
    """Best-effort error string from a Bot API body, falling back to the HTTP status."""
    try:
        body = response.json()
    except ValueError:
        body = None
    description = body.get("description") if isinstance(body, dict) else None
    return description or f"HTTP {response.status_code}"


def _is_transient_code(code: object) -> bool:
    """429 and 5xx are worth another attempt; every other failure code is not."""
    return code == 429 or (isinstance(code, int) and code >= 500)


def _tg_post(client: TGClient, method: str, payload: dict) -> dict:
    """POST to the Bot API and classify the outcome.

    Raises ``TelegramPermanentError`` for failures that will never succeed. Everything else
    propagates so the caller's retry ladder engages.
    """
    response = client.post(method, payload)
    status = response.status_code
    if status >= 400:
        error = _extract_tg_error(response)
        if _is_transient_code(status):
            logger.warning("Telegram %s returned HTTP %s (retryable): %s", method, status, error)
            raise TelegramTransientError(f"Telegram {method} failed with HTTP {status}: {error}")
        logger.warning("Telegram %s returned HTTP %s: %s", method, status, error)
        raise TelegramPermanentError(error)

    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        # A 2xx that is not a Bot API envelope means something interposed (a proxy, a captive
        # portal); the next attempt may well reach Telegram, so this is transient, not permanent.
        # Valid JSON that is not an object counts — a portal answering ``[]`` is still a portal.
        logger.warning("Telegram %s returned a non-envelope body with HTTP %s", method, status)
        raise TelegramTransportError(f"Telegram {method} returned a non-envelope body with HTTP {status}")

    if body.get("ok") is True:
        return body

    error_code = body.get("error_code")
    error = str(body.get("description") or f"error_code={error_code!r}")
    if _is_transient_code(error_code):
        logger.warning("Telegram %s 2xx envelope error %s (retryable): %s", method, error_code, error)
        raise TelegramTransientError(f"Telegram {method} failed with error_code={error_code!r}: {error}")
    logger.warning("Telegram %s 2xx envelope error %s: %s", method, error_code, error)
    raise TelegramPermanentError(error)
