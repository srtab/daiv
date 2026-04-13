from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import transaction

from notifications.channels.registry import get_channel
from notifications.choices import DeliveryStatus
from notifications.exceptions import UnknownChannelError
from notifications.models import Notification, NotificationDelivery

if TYPE_CHECKING:
    from collections.abc import Sequence

    from accounts.models import User

logger = logging.getLogger(__name__)


def _resolve_address_or_skipped_reason(channel_type: str, recipient: User) -> tuple[str, str | None]:
    """Return (address, skipped_reason). If skipped_reason is set, address is "" and the delivery
    should be created with status=skipped."""
    try:
        channel = get_channel(channel_type)
    except UnknownChannelError:
        return "", f"unknown channel: {channel_type}"

    address = channel.resolve_address(recipient)
    if address is None:
        return "", "no binding"
    return address, None


@transaction.atomic
def create_notification(
    *,
    recipient: User,
    event_type: str,
    source_type: str,
    source_id: str,
    subject: str,
    body: str,
    link_url: str,
    channels: Sequence[str],
    context: dict | None = None,
) -> Notification:
    """Create a Notification and one NotificationDelivery per channel. Does not dispatch."""
    notification = Notification.objects.create(
        recipient=recipient,
        event_type=event_type,
        source_type=source_type,
        source_id=source_id,
        subject=subject,
        body=body,
        link_url=link_url,
        context=context or {},
    )
    for channel_type in channels:
        address, skipped_reason = _resolve_address_or_skipped_reason(channel_type, recipient)
        NotificationDelivery.objects.create(
            notification=notification,
            channel_type=channel_type,
            address=address,
            status=DeliveryStatus.SKIPPED if skipped_reason else DeliveryStatus.PENDING,
            error_message=skipped_reason or "",
        )
    return notification
