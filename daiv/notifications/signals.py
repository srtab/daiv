from __future__ import annotations

import logging

from django.conf import settings
from django.db import Error as DatabaseError
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone

from sessions.signal_defs import run_classified, watch_exhausted

from notifications.choices import ChannelType
from notifications.models import UserChannelBinding
from notifications.run_notifiers import emit_run_notification
from notifications.schedule_notifiers import emit_schedule_dispatch_failed
from notifications.watch_notifiers import emit_watch_exhausted
from schedules.signals import schedule_dispatch_failed

logger = logging.getLogger("daiv.notifications")


@receiver(run_classified, dispatch_uid="notifications.on_run_classified")
def on_run_classified(sender, run, envelope, **kwargs) -> None:
    """Deliver notifications for a classified Run; swallow errors so the signal never propagates."""
    try:
        emit_run_notification(run, envelope)
    except Exception:
        logger.exception("on_run_classified: unexpected error for run=%s", getattr(run, "pk", run))


@receiver(watch_exhausted, dispatch_uid="notifications.on_watch_exhausted")
def on_watch_exhausted(sender, session, failing_jobs, pipeline_url, pipeline_id, **kwargs) -> None:
    """Tell the session owner the CI watch gave up; swallow errors so the signal never propagates."""
    try:
        emit_watch_exhausted(
            session=session, failing_jobs=failing_jobs, pipeline_url=pipeline_url, pipeline_id=pipeline_id
        )
    except Exception:
        logger.exception("on_watch_exhausted: unexpected error for thread_id=%s", session.thread_id)


@receiver(schedule_dispatch_failed, dispatch_uid="notifications.on_schedule_dispatch_failed")
def on_schedule_dispatch_failed(sender, schedule_pk, **kwargs) -> None:
    """Tell the schedule owner its dispatches are failing; swallow errors so the signal never propagates."""
    try:
        emit_schedule_dispatch_failed(schedule_pk)
    except Exception:
        logger.exception("on_schedule_dispatch_failed: unexpected error for schedule=%s", schedule_pk)


@receiver(post_save, sender=settings.AUTH_USER_MODEL, dispatch_uid="notifications.sync_email_binding")
def sync_email_binding(sender, instance, created, **kwargs) -> None:
    """Ensure the user always has a verified email channel binding.

    On creation, creates the initial binding. On update, syncs the binding address
    if the user's email has changed.
    """
    if not instance.email:
        return

    update_fields = kwargs.get("update_fields")
    if update_fields is not None and "email" not in update_fields:
        return

    try:
        binding = UserChannelBinding.objects.filter(user=instance, channel_type=ChannelType.EMAIL).first()
        if binding is None:
            UserChannelBinding.objects.create(
                user=instance,
                channel_type=ChannelType.EMAIL,
                address=instance.email,
                is_verified=True,
                verified_at=timezone.now(),
            )
        elif binding.address != instance.email:
            binding.address = instance.email
            binding.is_verified = True
            binding.verified_at = timezone.now()
            binding.save(update_fields=["address", "is_verified", "verified_at", "modified"])
    except DatabaseError:
        logger.exception("Failed to sync email binding for user %s (pk=%s)", instance, instance.pk)
