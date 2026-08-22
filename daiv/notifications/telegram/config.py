"""Bring Telegram's registered webhook in line with this instance's configuration.

Called from the configuration-save post-commit hook and from the reconcile cron. Reads the
*effective* token from ``site_settings`` — env-locked or DB-backed, same code path — because
an env-locked token never round-trips through the form and so never reaches ``clean``.
"""

from __future__ import annotations

import logging
import secrets

from django.urls import reverse
from django.utils.translation import gettext as _

from core.models import SiteConfiguration
from core.site_settings import site_settings
from core.utils import build_absolute_url
from notifications.telegram.client import TGClient

logger = logging.getLogger("daiv.notifications")

_WEBHOOK_SECRET_BYTES = 32


def webhook_url() -> str:
    """The URL Telegram should post updates to.

    The Sites framework is already the canonical source of the external URL, so no new
    base-URL setting is needed.
    """
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
        else:
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
        except Exception as exc:  # noqa: BLE001 — the save stands either way
            logger.warning("Telegram deleteWebhook failed: %s", exc)
            warnings.append(str(_("Could not remove the Telegram webhook: %(err)s") % {"err": exc}))
        return warnings

    config = SiteConfiguration.objects.get_instance()
    dirty = False

    try:
        username = (client.get_me().get("result") or {}).get("username")
    except Exception as exc:  # noqa: BLE001 — a warning, never a failed save
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
    # update until the secret lands. ``SiteConfiguration.save`` invalidates the read cache on
    # commit, keeping every process's next ``get_cached()`` fresh.
    if dirty:
        config.save()

    try:
        client.set_webhook(url=webhook_url(), secret_token=secret)
    except Exception as exc:  # noqa: BLE001 — a warning, never a failed save
        logger.warning("Telegram setWebhook failed: %s", exc)
        warnings.append(str(_("Could not register the Telegram webhook: %(err)s") % {"err": exc}))
    return warnings
