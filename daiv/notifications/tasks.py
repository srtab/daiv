from __future__ import annotations

import logging
from datetime import timedelta
from uuid import UUID

from django.db.models import Count, Q
from django.utils import timezone

from crontask import cron
from django_tasks import task

from core.utils import locked_task
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
    delivery.save(update_fields=["attempts", "last_attempted_at", "modified"])

    try:
        channel = get_channel(delivery.channel_type)
    except UnknownChannelError as exc:
        delivery.mark_skipped(str(exc))
        return

    try:
        channel.send(delivery.notification, delivery)
    except UnrecoverableDeliveryError as exc:
        logger.warning("Unrecoverable failure delivering %s: %s", delivery_id, exc)
        delivery.mark_failed(str(exc))
        return
    except Exception as exc:
        logger.exception(
            "Transient failure delivering %s via %s (attempt %d/%d)",
            delivery_id,
            delivery.channel_type,
            delivery.attempts,
            MAX_DELIVERY_ATTEMPTS,
        )
        if delivery.attempts >= MAX_DELIVERY_ATTEMPTS:
            delivery.mark_failed(str(exc))
            return
        # Stay PENDING; re-enqueue with backoff
        delivery.error_message = str(exc)
        delivery.save(update_fields=["error_message", "modified"])
        backoff = RETRY_BACKOFF_SECONDS[min(delivery.attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
        run_after = timezone.now() + timedelta(seconds=backoff)
        try:
            deliver_notification_task.using(run_after=run_after).enqueue(str(delivery.id))
        except Exception:
            logger.exception("Failed to re-enqueue delivery %s for retry", delivery_id)
            delivery.mark_failed(f"Re-enqueue failed after attempt {delivery.attempts}")
        return

    delivery.mark_sent()


@task()
def deliver_notification_task(delivery_id: str) -> None:
    """Public task entry point -- thin wrapper so tests can call ``_deliver_notification`` directly."""
    _deliver_notification(UUID(delivery_id))


REDRIVE_BATCH_LIMIT = 200


@cron("*/15 * * * *")
@task
@locked_task(key="redrive-missing-notifications")
def redrive_missing_notifications_cron_task():
    """Close the crash window between envelope-persist and run_classified delivery.

    If a worker dies after the envelope is written but before run_classified is delivered, the run has
    an envelope (so the reclassify backstop skips it) and the notification would be lost forever. This
    sweep re-emits run_classified for classified, notify-worthy, in-window, un-muted runs that are still
    missing at least one recipient's Notification row; the per-run/batch unique constraints make the
    re-emit idempotent, so recipients that already have a row are left untouched.

    ``locked_task`` (non-blocking) skips this tick if the prior one still holds the lock.
    """
    from sessions.models import Run, RunStatus
    from sessions.signals import get_classify_origins, run_classified
    from sessions.tasks import RECLASSIFY_MAX_AGE

    from notifications.models import Notification
    from notifications.signals import (
        _notify_worthy_statuses,
        _resolve_recipients_run,
        _within_relevance_window,
        notification_source_for_run,
    )

    def _delivered(source_type: str, source_id: str, event_type: str) -> set:
        return set(
            Notification.objects.filter(
                source_type=source_type, source_id=source_id, event_type=event_type
            ).values_list("recipient_id", flat=True)
        )

    now = timezone.now()
    candidates = (
        Run.objects
        .filter(
            trigger_type__in=get_classify_origins(),
            status__in=RunStatus.terminal(),
            envelope__isnull=False,
            envelope__status__in=_notify_worthy_statuses(),
            finished_at__isnull=False,
            finished_at__gte=now - RECLASSIFY_MAX_AGE,
        )
        .select_related("envelope", "session", "session__scheduled_job", "session__scheduled_job__user", "user")
        .order_by("finished_at")[:REDRIVE_BATCH_LIMIT]
    )

    redriven = 0
    for run in candidates:
        if not _within_relevance_window(run.finished_at) or run.effective_muted:
            continue
        # Skip only when EVERY expected recipient already has a row. A partial fan-out — a crash between
        # the per-recipient commits — must still be re-driven for the recipients that missed out.
        expected = set(_resolve_recipients_run(run))
        if not expected:
            continue
        # Resolve the notification key exactly as the emit did: a single-repo submit carries a
        # batch_id but is delivered per-run (the rollup needs >1 sibling). Keying it as a batch here
        # would look up a rollup that never exists and re-emit it on every tick.
        if run.batch_id is not None:
            counts = Run.objects.by_batch(run.batch_id).aggregate(
                total=Count("id"), classified=Count("id", filter=Q(envelope__isnull=False))
            )
            total = counts["total"]
            # A >1 batch rollup is only deliverable once every sibling is classified; an incomplete
            # batch is pending (the reclassify backstop will classify the stragglers), not stuck.
            if total > 1 and counts["classified"] < total:
                continue
        else:
            total = 1
        source_type, source_id, event_type = notification_source_for_run(run, total)
        if expected.issubset(_delivered(source_type, source_id, event_type)):
            continue
        run_classified.send_robust(sender=Run, run=run, envelope=run.envelope)
        # on_run_classified swallows its own errors, so send_robust never reports one — re-read the
        # rows instead. A recipient still missing after the re-emit is a genuinely stuck delivery
        # (a persistent create failure), not this tick's success; only a clean fan-out is counted.
        still_missing = expected - _delivered(source_type, source_id, event_type)
        if still_missing:
            logger.error(
                "redrive: run=%s still missing notification after re-emit for recipient_pk(s)=%s",
                run.pk,
                sorted(str(pk) for pk in still_missing),
            )
        else:
            redriven += 1

    if redriven:
        logger.info("redrive_missing_notifications: re-emitted run_classified for %d run(s)", redriven)
