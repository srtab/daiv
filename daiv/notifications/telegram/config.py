"""Bring Telegram's registered webhook in line with this instance's configuration.

Called from the configuration-save view once the save has committed, and from the reconcile
cron. Reads the *effective* token from ``site_settings`` — env-locked or DB-backed, same code
path — because an env-locked token never round-trips through the form and so never reaches
``clean``.
"""

from __future__ import annotations

import logging
import secrets

from django.urls import reverse
from django.utils.translation import gettext as _

from core.models import SiteConfiguration
from core.site_settings import site_settings
from core.utils import build_absolute_url
from notifications.telegram.client import TelegramError, TGClient

logger = logging.getLogger("daiv.notifications")

_WEBHOOK_SECRET_BYTES = 32


def webhook_url() -> str:
    """The URL Telegram should post updates to.

    The Sites framework is already the canonical source of the external URL, so no new
    base-URL setting is needed.
    """
    # A deliberate string coupling to a route owned by ``notifications/api/views.py``: no
    # import-graph check can see it, so extracting this package must move that route too.
    return build_absolute_url(reverse("api:telegram_callback"))


def sync_telegram() -> list[str]:
    """Derive the bot username, ensure a webhook secret, and (de)register the webhook.

    Returns user-facing warning strings; it does not raise on a Telegram or network failure,
    because an outage must not block an unrelated configuration edit and the save it follows
    has already committed.
    """
    warnings: list[str] = []
    enabled = bool(site_settings.telegram_enabled)
    client = TGClient.from_site_settings()

    if client is None:
        if enabled:
            warnings.append(str(_("Telegram is enabled but no bot token is configured.")))
        elif site_settings.telegram_webhook_secret:
            # Only a successful sync ever writes the secret, so it is the "a webhook was once
            # registered" flag. Without it there is nothing in BotFather to go and clean up.
            warnings.append(
                str(
                    _(
                        "The bot token was removed while the channel is disabled. The Telegram webhook could not be "
                        "deregistered. Remove it in BotFather, or restore the token and disable the channel again."
                    )
                )
            )
        return warnings

    if not enabled:
        # The stored secret is deliberately retained: the webhook route stays fail-closed, and
        # answering a straggling update 2xx rather than 401 needs the secret to still match.
        try:
            client.delete_webhook()
        except TelegramError as exc:
            logger.warning("Telegram deleteWebhook failed: %s", exc)
            warnings.append(str(_("Could not remove the Telegram webhook: %(err)s") % {"err": exc}))
        return warnings

    config = SiteConfiguration.objects.get_instance()
    dirty = False

    try:
        result = client.get_me().get("result")
        username = result.get("username") if isinstance(result, dict) else None
    except TelegramError as exc:
        logger.warning("Telegram getMe failed: %s", exc)
        warnings.append(str(_("Could not read the Telegram bot username: %(err)s") % {"err": exc}))
        username = None

    # Re-derived on every sync rather than only when blank: getMe is authoritative and trivial,
    # and unconditional re-derivation self-heals a rotated token. Skipped when env-locked,
    # where the DB value is shadowed on read anyway.
    if (
        username
        and not site_settings.is_env_locked("telegram_bot_username")
        and config.telegram_bot_username != username
    ):
        config.telegram_bot_username = username
        dirty = True

    stored = site_settings.telegram_webhook_secret
    secret = stored.get_secret_value() if stored else ""
    if not secret:
        secret = secrets.token_urlsafe(_WEBHOOK_SECRET_BYTES)
        config.telegram_webhook_secret = secret
        dirty = True

    # Persisted BEFORE setWebhook carries it: the route is fail-closed, so it rejects every
    # update until the secret lands. Scoped to these two columns because the row was read before
    # a getMe that can take seconds, and a full save would revert a concurrent edit elsewhere.
    if dirty:
        config.save(update_fields=["telegram_bot_username", "_telegram_webhook_secret_encrypted"])

    # Resolved outside the try so a NoReverseMatch or a Site misconfiguration is not reported as
    # a Telegram outage; the callers own that case.
    url = webhook_url()
    try:
        client.set_webhook(url=url, secret_token=secret)
    except TelegramError as exc:
        logger.warning("Telegram setWebhook failed: %s", exc)
        warnings.append(str(_("Could not register the Telegram webhook: %(err)s") % {"err": exc}))
    return warnings
