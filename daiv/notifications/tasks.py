from __future__ import annotations

import logging
from datetime import timedelta
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from django_tasks import task

from notifications.channels.registry import get_channel
from notifications.choices import DeliveryStatus
from notifications.exceptions import UnknownChannelError, UnrecoverableDeliveryError
from notifications.models import NotificationDelivery

logger = logging.getLogger("daiv.notifications")

MAX_DELIVERY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = [60, 300]  # wait before attempt 2, before attempt 3


def _deliver_notification(delivery_id: UUID) -> None:
    """Execute a single delivery attempt. Called by the django-tasks worker and by tests."""
    try:
        delivery = NotificationDelivery.objects.select_related("notification__recipient").get(id=delivery_id)
    except NotificationDelivery.DoesNotExist:
        logger.warning("Delivery %s no longer exists, skipping", delivery_id)
        return

    if delivery.status != DeliveryStatus.PENDING:
        logger.info("Delivery %s has status=%s, skipping", delivery_id, delivery.status)
        return

    delivery.attempts += 1
    delivery.last_attempted_at = timezone.now()

    try:
        channel = get_channel(delivery.channel_type)
    except UnknownChannelError as exc:
        delivery.status = DeliveryStatus.SKIPPED
        delivery.error_message = str(exc)
        delivery.save(update_fields=["status", "error_message", "attempts", "last_attempted_at"])
        return

    try:
        channel.send(delivery.notification, delivery)
    except UnrecoverableDeliveryError as exc:
        logger.warning("Unrecoverable failure delivering %s: %s", delivery_id, exc)
        delivery.status = DeliveryStatus.FAILED
        delivery.error_message = str(exc)
        delivery.save(update_fields=["status", "error_message", "attempts", "last_attempted_at"])
        return
    except Exception as exc:
        logger.exception(
            "Transient failure delivering %s via %s (attempt %d/%d)",
            delivery_id,
            delivery.channel_type,
            delivery.attempts,
            MAX_DELIVERY_ATTEMPTS,
        )
        delivery.error_message = str(exc)
        if delivery.attempts >= MAX_DELIVERY_ATTEMPTS:
            delivery.status = DeliveryStatus.FAILED
            delivery.save(update_fields=["status", "error_message", "attempts", "last_attempted_at"])
            return
        # Stay PENDING; re-enqueue with backoff
        delivery.save(update_fields=["error_message", "attempts", "last_attempted_at"])
        backoff = RETRY_BACKOFF_SECONDS[min(delivery.attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
        run_after = timezone.now() + timedelta(seconds=backoff)
        transaction.on_commit(lambda: deliver_notification_task.using(run_after=run_after).enqueue(str(delivery.id)))
        return

    delivery.status = DeliveryStatus.SENT
    delivery.delivered_at = timezone.now()
    delivery.error_message = ""
    delivery.save(update_fields=["status", "delivered_at", "error_message", "attempts", "last_attempted_at"])


@task()
def deliver_notification_task(delivery_id: str) -> None:
    """Public task entry point -- thin wrapper so tests can call ``_deliver_notification`` directly."""
    _deliver_notification(UUID(delivery_id))
