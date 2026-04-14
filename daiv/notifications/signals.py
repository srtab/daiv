from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import Error as DatabaseError
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from activity.signals import activity_finished

from notifications.channels.registry import all_channels
from notifications.choices import ChannelType, NotifyOn
from notifications.models import UserChannelBinding
from notifications.services import notify

if TYPE_CHECKING:
    from activity.models import Activity

logger = logging.getLogger("daiv.notifications")


def _status_matches(notify_on: str, status: str) -> bool:
    from activity.models import ActivityStatus

    if notify_on == NotifyOn.NEVER:
        return False
    if notify_on == NotifyOn.ALWAYS:
        return status in ActivityStatus.terminal()
    if notify_on == NotifyOn.ON_SUCCESS:
        return status == ActivityStatus.SUCCESSFUL
    if notify_on == NotifyOn.ON_FAILURE:
        return status == ActivityStatus.FAILED
    return False


def _render_subject(schedule, activity) -> str:
    from activity.models import ActivityStatus

    if activity.status == ActivityStatus.SUCCESSFUL:
        return _("Scheduled job '%(name)s' succeeded") % {"name": schedule.name}
    return _("Scheduled job '%(name)s' failed") % {"name": schedule.name}


def _render_body(schedule, activity) -> str:
    from activity.models import ActivityStatus

    if activity.status == ActivityStatus.SUCCESSFUL:
        body = _("Your scheduled job '%(name)s' finished successfully.") % {"name": schedule.name}
        if activity.result_summary:
            body += "\n\n" + activity.result_summary
    else:
        body = _("Your scheduled job '%(name)s' failed.") % {"name": schedule.name}
        if activity.error_message:
            body += "\n\n" + activity.error_message
    return body


@receiver(activity_finished, dispatch_uid="notifications.on_activity_finished")
def on_activity_finished(sender, activity: Activity, **kwargs) -> None:
    schedule = activity.scheduled_job
    if schedule is None or schedule.notify_on == NotifyOn.NEVER:
        return
    if not _status_matches(schedule.notify_on, activity.status):
        return

    channels = [cls.channel_type for cls in all_channels()]
    if not channels:
        return

    try:
        notify(
            recipient=schedule.user,
            event_type="schedule.finished",
            source_type="activity.Activity",
            source_id=str(activity.pk),
            subject=_render_subject(schedule, activity),
            body=_render_body(schedule, activity),
            link_url=reverse("activity_detail", args=[activity.pk]),
            channels=channels,
            context={"status": activity.status, "schedule_name": schedule.name},
        )
    except Exception:
        logger.exception("Failed to create notification for activity %s", activity.pk)


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
