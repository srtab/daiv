from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils.translation import gettext_lazy as _

from core.site_settings import site_settings
from core.utils import build_absolute_url
from notifications.channels.base import NotificationChannel
from notifications.channels.registry import register_channel
from notifications.channels.telegram_renderers.registry import get_renderer
from notifications.choices import ChannelType
from notifications.exceptions import UnrecoverableDeliveryError
from notifications.telegram.client import TelegramPermanentError, TGClient, is_blocked_error
from notifications.telegram_bindings import unverify_binding

if TYPE_CHECKING:
    from notifications.models import Notification, NotificationDelivery

logger = logging.getLogger("daiv.notifications")


def _compose_text(notification: Notification) -> str:
    # Local copy, not shared: rocketchat's version binds build_absolute_url by module path in its
    # test suite (rocketchat.build_absolute_url); hoisting would delete that patch target.
    parts = [notification.subject, "", notification.body]
    if notification.link_url:
        parts.extend(["", build_absolute_url(notification.link_url)])
    return "\n".join(parts)


def _build_payload(notification: Notification, delivery: NotificationDelivery) -> dict:
    """Build the ``sendMessage`` body.

    Falls back to plain text when no renderer is registered for the event type, so a
    newly-introduced event delivers before its renderer ships.
    """
    renderer = get_renderer(notification.event_type)
    if renderer is None:
        logger.warning("Telegram: no renderer for event_type=%r; sending plain text", notification.event_type)
        return {"chat_id": delivery.address, "text": _compose_text(notification)}
    text, reply_markup = renderer.render(notification)
    payload: dict = {"chat_id": delivery.address, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return payload


@register_channel
class TelegramChannel(NotificationChannel):
    channel_type = ChannelType.TELEGRAM
    display_name = _("Telegram")

    @classmethod
    def is_enabled(cls) -> bool:
        return bool(site_settings.telegram_enabled)

    def send(self, notification: Notification, delivery: NotificationDelivery) -> None:
        if not self.is_enabled():
            raise UnrecoverableDeliveryError("Telegram is disabled")

        client = TGClient.from_site_settings()
        if client is None:
            raise UnrecoverableDeliveryError("Telegram not configured")

        try:
            client.send_message(_build_payload(notification, delivery))
        except TelegramPermanentError as exc:
            if is_blocked_error(str(exc)):
                # Never recovers on its own; flip the binding so resolve_address returns None
                # and later notifications record as skipped instead of burning three retries.
                flipped = unverify_binding(notification.recipient_id, delivery.address)
                logger.warning(
                    "Telegram delivery %s: chat %s blocked the bot; unverified %d binding(s)",
                    delivery.id,
                    delivery.address,
                    flipped,
                )
            logger.error("Telegram delivery %s permanently failed: %s", delivery.id, exc)
            raise UnrecoverableDeliveryError(str(exc)) from exc
