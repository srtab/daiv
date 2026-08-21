from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from core.site_settings import site_settings

logger = logging.getLogger("daiv.notifications")

_TG_API_BASE = "https://api.telegram.org"
_TG_TIMEOUT_SECONDS = 5.0

# Telegram exposes no machine code for a blocked bot, so the flip in
# ``TelegramChannel.send`` matches on this wording. Acknowledged fragile: if it drifts the
# flip stops firing and the delivery is what it would have been anyway — a permanent FAILED.
_TG_BLOCKED_DESCRIPTION = "bot was blocked by the user"

# The only update kinds the webhook handles. Narrowing the subscription keeps Telegram from
# spending retries on updates the route answers 204 to.
ALLOWED_UPDATES = ["message", "my_chat_member"]


class TelegramPermanentError(Exception):
    """Raised when Telegram returns a failure that will never succeed on retry."""


def is_blocked_error(description: str) -> bool:
    """True when a permanent failure is specifically "the user blocked this bot"."""
    return _TG_BLOCKED_DESCRIPTION in description.lower()


@dataclass(frozen=True)
class TGClient:
    """The bot token plus convenience Bot API calls.

    ``token`` is excluded from the default repr: it is a full-control credential and would
    otherwise reach log lines, tracebacks, and test output.
    """

    token: str = field(repr=False)

    @classmethod
    def from_site_settings(cls) -> TGClient | None:
        token = site_settings.telegram_bot_token
        if not token:
            return None
        return cls(token=token.get_secret_value())

    def post(self, method: str, payload: dict) -> httpx.Response:
        return httpx.post(f"{_TG_API_BASE}/bot{self.token}/{method}", json=payload, timeout=_TG_TIMEOUT_SECONDS)

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
        body = {}
    return body.get("description") or f"HTTP {response.status_code}"


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
            raise RuntimeError(f"Telegram {method} failed with HTTP {status}: {error}")
        logger.warning("Telegram %s returned HTTP %s: %s", method, status, error)
        raise TelegramPermanentError(error)

    body = response.json()
    if body.get("ok") is True:
        return body

    error_code = body.get("error_code")
    error = str(body.get("description") or f"error_code={error_code!r}")
    if _is_transient_code(error_code):
        logger.warning("Telegram %s 2xx envelope error %s (retryable): %s", method, error_code, error)
        raise RuntimeError(f"Telegram {method} failed with error_code={error_code!r}: {error}")
    logger.warning("Telegram %s 2xx envelope error %s: %s", method, error_code, error)
    raise TelegramPermanentError(error)
