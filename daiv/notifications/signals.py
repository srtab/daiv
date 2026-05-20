from __future__ import annotations

import logging

from django.conf import settings
from django.db import Error as DatabaseError
from django.db import IntegrityError
from django.db.models import Count, Q
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from activity.models import Activity, ActivityStatus, TriggerType
from activity.signals import activity_finished

from notifications.channels.registry import enabled_channels
from notifications.choices import ChannelType, EventType, NotifyOn
from notifications.models import UserChannelBinding
from notifications.services import notify

logger = logging.getLogger("daiv.notifications")

EXCLUDED_TRIGGERS = {TriggerType.ISSUE_WEBHOOK, TriggerType.MR_WEBHOOK}


def _is_schedule(activity: Activity) -> bool:
    """True when ``activity`` is linked to a still-loadable ScheduledJob.

    Both checks are required: the FK uses ``on_delete=SET_NULL``, so an instance can
    have a non-null ``scheduled_job_id`` but a ``None`` related object after the
    ScheduledJob is deleted out from under it.
    """
    return activity.scheduled_job_id is not None and activity.scheduled_job is not None


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
    if _is_schedule(activity):
        schedule = activity.scheduled_job
        recipients: dict[int, object] = {schedule.user_id: schedule.user}
        for sub in schedule.subscribers.all():
            recipients.setdefault(sub.pk, sub)
        return recipients
    if activity.user is not None:
        return {activity.user.pk: activity.user}
    return {}


def _render_payload(activity: Activity) -> tuple[str, str, dict]:
    is_schedule = _is_schedule(activity)
    ok = activity.status == ActivityStatus.SUCCESSFUL
    repo = activity.repo_id
    name = activity.scheduled_job.name if is_schedule else ""
    # Owner disambiguates schedules that share a name across users.
    owner = str(activity.scheduled_job.user) if is_schedule else ""

    if is_schedule:
        params = {"name": name, "owner": owner, "repo": repo}
        if ok:
            subject = _("'%(name)s' succeeded on %(repo)s — %(owner)s") % params
            body = _("Scheduled run '%(name)s' by %(owner)s finished on %(repo)s.") % params
        else:
            subject = _("'%(name)s' failed on %(repo)s — %(owner)s") % params
            body = _("Scheduled run '%(name)s' by %(owner)s failed on %(repo)s.") % params
    else:
        if ok:
            subject = _("Agent run on %(repo)s succeeded") % {"repo": repo}
            body = _("Agent run on %(repo)s finished successfully.") % {"repo": repo}
        else:
            subject = _("Agent run on %(repo)s failed") % {"repo": repo}
            body = _("Agent run on %(repo)s failed.") % {"repo": repo}

    context = {
        "status": activity.status,
        "status_label": activity.get_status_display(),
        "is_successful": ok,
        "trigger_label": activity.get_trigger_type_display(),
        "trigger_name": name,
        "trigger_owner": owner,
        "repo_id": repo,
        "duration_seconds": activity.duration,
    }
    return subject, body, context


@receiver(activity_finished, dispatch_uid="notifications.on_activity_finished")
def on_activity_finished(sender, activity: Activity, **kwargs) -> None:
    if activity.trigger_type in EXCLUDED_TRIGGERS:
        return

    if activity.batch_id is not None:
        siblings = Activity.objects.by_batch(activity.batch_id)
        total = siblings.count()
        if total > 1:
            _handle_batch_completion(activity, siblings, total)
            return

    recipients = _resolve_recipients(activity)
    if not recipients:
        return

    effective = activity.effective_notify_on
    # The Notification row doubles as the in-app bell entry and is always written for
    # terminal activities with a recipient. ``notify_on`` only gates external delivery
    # channels (email, etc.) — empty channels list means bell-only, no external dispatch.
    channels = [cls.channel_type for cls in enabled_channels()] if _status_matches(effective, activity.status) else []

    subject, body, context = _render_payload(activity)
    link_url = reverse("activity_detail", args=[activity.pk])
    event_type = EventType.SCHEDULE_FINISHED if _is_schedule(activity) else EventType.JOB_FINISHED

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


def _handle_batch_completion(activity: Activity, siblings, total: int) -> None:
    """Emit a single rollup notification when every sibling in the batch is terminal.

    Sibling-level notifications are suppressed entirely for multi-job batches; only
    this rollup is written. Two near-simultaneous "last" workers can both see the
    batch as complete — the partial unique constraint on ``Notification`` lets the DB
    elect a single winner and the loser swallows ``IntegrityError`` below.
    """
    agg = siblings.aggregate(
        terminal=Count("id", filter=Q(status__in=ActivityStatus.terminal())),
        successful=Count("id", filter=Q(status=ActivityStatus.SUCCESSFUL)),
    )
    if agg["terminal"] < total:
        return

    recipients = _resolve_recipients(activity)
    if not recipients:
        # A multi-job batch finalizing with zero recipients usually means a misconfigured
        # schedule or a deleted user — worth surfacing so an operator can investigate.
        logger.warning(
            "Batch %s completed with no resolvable recipients (activity_pk=%s, total=%d)",
            activity.batch_id,
            activity.pk,
            total,
        )
        return

    successful = agg["successful"]
    failed = total - successful
    agg_status = ActivityStatus.SUCCESSFUL if failed == 0 else ActivityStatus.FAILED

    rows = list(siblings.values_list("repo_id", "started_at", "finished_at"))

    effective = activity.effective_notify_on
    channels = [cls.channel_type for cls in enabled_channels()] if _status_matches(effective, agg_status) else []

    subject, body, context = _render_batch_payload(activity, rows, total, successful, failed, agg_status)
    link_url = f"{reverse('activity_list')}?batch={activity.batch_id}"

    for recipient in recipients.values():
        try:
            notify(
                recipient=recipient,
                event_type=EventType.JOB_BATCH_FINISHED,
                source_type="activity.Batch",
                source_id=str(activity.batch_id),
                subject=subject,
                body=body,
                link_url=link_url,
                channels=channels,
                context=context,
            )
        except IntegrityError:
            # Distinguish the expected race (sibling worker already inserted the rollup)
            # from any other integrity violation (FK to a deleted recipient, NOT NULL, etc.).
            # The expected race leaves a matching row behind; anything else does not.
            if _rollup_exists(recipient, activity.batch_id):
                logger.debug(
                    "Batch rollup already exists for batch_id=%s recipient_pk=%s",
                    activity.batch_id,
                    getattr(recipient, "pk", None),
                )
            else:
                logger.exception(
                    "Unexpected IntegrityError creating batch notification for batch_id=%s recipient pk=%s",
                    activity.batch_id,
                    getattr(recipient, "pk", None),
                )
        except Exception:
            logger.exception(
                "Failed to create batch notification for batch_id=%s recipient pk=%s",
                activity.batch_id,
                getattr(recipient, "pk", None),
            )


def _rollup_exists(recipient, batch_id) -> bool:
    from notifications.models import Notification

    return Notification.objects.filter(
        recipient=recipient,
        source_type="activity.Batch",
        source_id=str(batch_id),
        event_type=EventType.JOB_BATCH_FINISHED,
    ).exists()


def _render_batch_payload(
    activity: Activity, rows: list[tuple], total: int, successful: int, failed: int, agg_status: str
) -> tuple[str, str, dict]:
    is_schedule = _is_schedule(activity)
    ok = failed == 0
    repo_ids = sorted({repo for repo, _start, _end in rows if repo})
    name = activity.scheduled_job.name if is_schedule else ""
    owner = str(activity.scheduled_job.user) if is_schedule else ""

    if is_schedule:
        params = {"name": name, "owner": owner, "total": total, "ok": successful, "failed": failed}
        if ok:
            subject = _("'%(name)s' batch succeeded (%(total)d runs) — %(owner)s") % params
            body = _("All %(total)d runs of '%(name)s' by %(owner)s finished successfully.") % params
        elif successful == 0:
            subject = _("'%(name)s' batch failed (%(total)d runs) — %(owner)s") % params
            body = _("All %(total)d runs of '%(name)s' by %(owner)s failed.") % params
        else:
            subject = _("'%(name)s' batch: %(ok)d/%(total)d succeeded — %(owner)s") % params
            body = _("%(ok)d of %(total)d runs of '%(name)s' by %(owner)s succeeded; %(failed)d failed.") % params
    else:
        repo_summary = _summarize_repos(repo_ids)
        if ok:
            subject = _("Agent run batch succeeded (%(total)d runs)") % {"total": total}
            body = _("All %(total)d runs on %(repos)s finished successfully.") % {"total": total, "repos": repo_summary}
        elif successful == 0:
            subject = _("Agent run batch failed (%(total)d runs)") % {"total": total}
            body = _("All %(total)d runs on %(repos)s failed.") % {"total": total, "repos": repo_summary}
        else:
            subject = _("Agent run batch finished: %(ok)d/%(total)d succeeded") % {"ok": successful, "total": total}
            body = _("%(ok)d of %(total)d runs on %(repos)s succeeded; %(failed)d failed.") % {
                "ok": successful,
                "total": total,
                "repos": repo_summary,
                "failed": failed,
            }

    context = {
        "status": str(agg_status),
        "status_label": str(ActivityStatus(agg_status).label),
        "is_successful": ok,
        "trigger_label": str(activity.get_trigger_type_display()),
        "trigger_name": name,
        "trigger_owner": owner,
        "repo_id": repo_ids[0] if len(repo_ids) == 1 else "",
        "repo_ids": repo_ids,
        "total": total,
        "successful_count": successful,
        "failed_count": failed,
        "duration_seconds": _batch_duration(rows),
        "batch_id": str(activity.batch_id),
    }
    return subject, body, context


def _summarize_repos(repo_ids: list[str], limit: int = 3) -> str:
    if not repo_ids:
        return ""
    if len(repo_ids) <= limit:
        return ", ".join(repo_ids)
    head = ", ".join(repo_ids[:limit])
    return _("%(repos)s and %(remaining)d more") % {"repos": head, "remaining": len(repo_ids) - limit}


def _batch_duration(rows: list[tuple]) -> float | None:
    """Wall-clock span from earliest start to latest finish across the batch."""
    pairs = [(start, end) for _repo, start, end in rows if start and end]
    if not pairs:
        return None
    earliest = min(start for start, _end in pairs)
    latest = max(end for _start, end in pairs)
    return (latest - earliest).total_seconds()


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
