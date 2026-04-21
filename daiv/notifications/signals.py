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

from activity.models import ActivityStatus, TriggerType
from activity.signals import activity_finished

from notifications.channels.registry import all_channels
from notifications.choices import ChannelType, NotifyOn
from notifications.models import UserChannelBinding
from notifications.services import notify

if TYPE_CHECKING:
    from activity.models import Activity

logger = logging.getLogger("daiv.notifications")

EXCLUDED_TRIGGERS = {TriggerType.ISSUE_WEBHOOK, TriggerType.MR_WEBHOOK}


def _status_matches(notify_on: NotifyOn, status: str) -> bool:
    if notify_on == NotifyOn.NEVER:
        return False
    if notify_on == NotifyOn.ALWAYS:
        return status in ActivityStatus.terminal()
    if notify_on == NotifyOn.ON_SUCCESS:
        return status == ActivityStatus.SUCCESSFUL
    if notify_on == NotifyOn.ON_FAILURE:
        return status == ActivityStatus.FAILED
    logger.warning("Unknown notify_on value %r; treating as NEVER", notify_on)
    return False


def _resolve_recipients(activity: Activity) -> dict[int, object]:
    if activity.scheduled_job_id is not None and activity.scheduled_job is not None:
        schedule = activity.scheduled_job
        recipients: dict[int, object] = {schedule.user_id: schedule.user}
        for sub in schedule.subscribers.all():
            recipients.setdefault(sub.pk, sub)
        return recipients
    if activity.user is not None:
        return {activity.user.pk: activity.user}
    return {}


def _render_payload(activity: Activity) -> tuple[str, str, dict]:
    is_schedule = activity.scheduled_job_id is not None and activity.scheduled_job is not None
    ok = activity.status == ActivityStatus.SUCCESSFUL

    if is_schedule:
        name = activity.scheduled_job.name
        if ok:
            subject = _("Scheduled job '%(name)s' succeeded") % {"name": name}
            body = _("Your scheduled job '%(name)s' finished successfully.") % {"name": name}
        else:
            subject = _("Scheduled job '%(name)s' failed") % {"name": name}
            body = _("Your scheduled job '%(name)s' failed.") % {"name": name}
    else:
        if ok:
            subject = _("Agent run on %(repo)s succeeded") % {"repo": activity.repo_id}
            body = _("Your agent run on '%(repo)s' finished successfully.") % {"repo": activity.repo_id}
        else:
            subject = _("Agent run on %(repo)s failed") % {"repo": activity.repo_id}
            body = _("Your agent run on '%(repo)s' failed.") % {"repo": activity.repo_id}

    context = {
        "status": activity.status,
        "status_label": activity.get_status_display(),
        "is_successful": ok,
        "trigger_label": activity.get_trigger_type_display(),
        "trigger_name": activity.scheduled_job.name if is_schedule else "",
        "repo_id": activity.repo_id,
        "duration_seconds": activity.duration,
    }
    return subject, body, context


@receiver(activity_finished, dispatch_uid="notifications.on_activity_finished")
def on_activity_finished(sender, activity: Activity, **kwargs) -> None:
    if activity.trigger_type in EXCLUDED_TRIGGERS:
        return

    recipients = _resolve_recipients(activity)
    if not recipients:
        return

    effective = activity.effective_notify_on
    # The Notification row doubles as the in-app bell entry and is always written for
    # terminal activities with a recipient. ``notify_on`` only gates external delivery
    # channels (email, etc.) — empty channels list means bell-only, no external dispatch.
    channels = [cls.channel_type for cls in all_channels()] if _status_matches(effective, activity.status) else []

    subject, body, context = _render_payload(activity)
    link_url = reverse("activity_detail", args=[activity.pk])
    event_type = "schedule.finished" if activity.scheduled_job_id is not None else "job.finished"

    for recipient in recipients.values():
        try:
            notify(
                recipient=recipient,
                event_type=event_type,
                source_type="activity.Activity",
                source_id=str(activity.pk),
                subject=subject,
                body=body,
                link_url=link_url,
                channels=channels,
                context=context,
            )
        except Exception:
            logger.exception(
                "Failed to create notification for activity %s, recipient pk=%s",
                activity.pk,
                getattr(recipient, "pk", None),
            )


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
