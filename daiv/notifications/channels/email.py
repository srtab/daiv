from __future__ import annotations

import logging
from smtplib import SMTPRecipientsRefused
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.mail import BadHeaderError, send_mail
from django.template import TemplateDoesNotExist, TemplateSyntaxError
from django.template.loader import render_to_string
from django.utils.translation import gettext_lazy as _

from notifications.channels.base import NotificationChannel
from notifications.channels.registry import register_channel
from notifications.choices import ChannelType
from notifications.exceptions import UnrecoverableDeliveryError

if TYPE_CHECKING:
    from notifications.models import Notification, NotificationDelivery

logger = logging.getLogger(__name__)


@register_channel
class EmailChannel(NotificationChannel):
    channel_type = ChannelType.EMAIL
    display_name = _("Email")

    def send(self, notification: Notification, delivery: NotificationDelivery) -> None:
        from core.utils import build_absolute_url, prefixed_email_subject

        link_absolute_url = build_absolute_url(notification.link_url) if notification.link_url else ""
        context = {"notification": notification, "link_absolute_url": link_absolute_url}
        try:
            text_body = render_to_string("notifications/emails/notification.txt", context)
            html_body = render_to_string("notifications/emails/notification.html", context)
        except (TemplateDoesNotExist, TemplateSyntaxError) as exc:
            raise UnrecoverableDeliveryError(f"Email template error: {exc}") from exc

        try:
            send_mail(
                subject=prefixed_email_subject(notification.subject),
                message=text_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[delivery.address],
                html_message=html_body,
            )
        except (SMTPRecipientsRefused, BadHeaderError) as exc:
            raise UnrecoverableDeliveryError(f"Permanent email failure: {exc}") from exc
