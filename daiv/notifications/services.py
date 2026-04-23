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
    from notifications.choices import ChannelType

logger = logging.getLogger(__name__)


def _resolve_address_or_skipped_reason(channel_type: ChannelType, recipient: User) -> tuple[str, str | None]:
    """Resolve the delivery address for a channel, or explain why it cannot be resolved.

    Returns (address, None) on success, or ("", reason) when the channel is unknown
    or the user has no binding."""
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
    channels: Sequence[ChannelType],
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


def dispatch_notification(notification: Notification) -> None:
    """Enqueue delivery tasks for each pending delivery on ``notification``."""
    from notifications.tasks import deliver_notification_task

    pending = notification.deliveries.filter(status=DeliveryStatus.PENDING).values_list("id", flat=True)
    for delivery_id in pending:
        try:
            deliver_notification_task.enqueue(str(delivery_id))
        except Exception:
            logger.exception("Failed to enqueue delivery task for delivery_id=%s", delivery_id)
            NotificationDelivery.objects.filter(id=delivery_id).update(
                status=DeliveryStatus.FAILED, error_message="Failed to enqueue delivery task"
            )


def notify(
    *,
    recipient: User,
    event_type: str,
    source_type: str = "",
    source_id: str = "",
    subject: str,
    body: str,
    link_url: str = "",
    channels: Sequence[ChannelType],
    context: dict | None = None,
) -> Notification:
    """Public API for triggering a notification. Creates the notification and schedules delivery
    on transaction commit."""
    notification = create_notification(
        recipient=recipient,
        event_type=event_type,
        source_type=source_type,
        source_id=source_id,
        subject=subject,
        body=body,
        link_url=link_url,
        channels=channels,
        context=context,
    )
    transaction.on_commit(lambda: dispatch_notification(notification))
    return notification
