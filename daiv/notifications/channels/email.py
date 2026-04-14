from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils.translation import gettext_lazy as _

from notifications.channels.base import NotificationChannel
from notifications.channels.registry import register_channel
from notifications.models import UserChannelBinding

if TYPE_CHECKING:
    from accounts.models import User
    from notifications.models import Notification, NotificationDelivery

logger = logging.getLogger(__name__)


@register_channel
class EmailChannel(NotificationChannel):
    channel_type = "email"
    display_name = _("Email")

    def resolve_address(self, user: User) -> str | None:
        binding = (
            UserChannelBinding.objects
            .filter(user=user, channel_type=self.channel_type, is_verified=True)
            .order_by("-updated_at")
            .first()
        )
        return binding.address if binding else None

    def send(self, notification: Notification, delivery: NotificationDelivery) -> None:
        from core.utils import build_absolute_url

        link_absolute_url = build_absolute_url(notification.link_url) if notification.link_url else ""
        context = {"notification": notification, "link_absolute_url": link_absolute_url}
        text_body = render_to_string("notifications/emails/notification.txt", context)
        html_body = render_to_string("notifications/emails/notification.html", context)
        send_mail(
            subject=notification.subject,
            message=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[delivery.address],
            html_message=html_body,
        )
