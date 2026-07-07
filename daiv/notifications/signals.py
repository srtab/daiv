from __future__ import annotations

import logging

from django.conf import settings
from django.db import Error as DatabaseError
from django.db import IntegrityError
from django.db.models import Count, Q, Sum
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from sessions.signals import run_finished

from notifications.channels.registry import enabled_channels
from notifications.choices import ChannelType, EventType, NotifyOn
from notifications.models import UserChannelBinding
from notifications.services import notify

logger = logging.getLogger("daiv.notifications")

EXCLUDED_RUN_TRIGGERS = {"issue_webhook", "mr_webhook"}


def _summarize_repos(repo_ids: list[str], limit: int = 3) -> str:
    if not repo_ids:
        return ""
    if len(repo_ids) <= limit:
        return ", ".join(repo_ids)
    head = ", ".join(repo_ids[:limit])
    return _("%(repos)s and %(remaining)d more") % {"repos": head, "remaining": len(repo_ids) - limit}


def _batch_duration(rows: list[tuple]) -> float | None:
    """Wall-clock span from earliest start to latest finish across the batch."""
    pairs = [(start, end) for _repo, start, end, _status in rows if start and end]
    if not pairs:
        return None
    earliest = min(start for start, _end in pairs)
    latest = max(end for _start, end in pairs)
    return (latest - earliest).total_seconds()


def _is_schedule_run(run) -> bool:
    """True when ``run`` belongs to a session with a still-loadable ScheduledJob."""
    session = run.session if run.session_id else None
    return session is not None and session.scheduled_job_id is not None and session.scheduled_job is not None


def _status_matches_run(notify_on: NotifyOn, status: str) -> bool:
    from sessions.models import RunStatus

    if notify_on == NotifyOn.NEVER:
        return False
    if notify_on == NotifyOn.ALWAYS:
        return status in RunStatus.terminal()
    if notify_on == NotifyOn.ON_SUCCESS:
        return status == RunStatus.SUCCESSFUL
    if notify_on == NotifyOn.ON_FAILURE:
        return status == RunStatus.FAILED
    logger.warning("Unknown notify_on value %r; treating as NEVER", notify_on)
    return False


def _resolve_recipients_run(run) -> dict[int, object]:
    if _is_schedule_run(run):
        schedule = run.session.scheduled_job
        recipients: dict[int, object] = {schedule.user_id: schedule.user}
        for sub in schedule.subscribers.all():
            recipients.setdefault(sub.pk, sub)
        return recipients
    if run.user is not None:
        return {run.user.pk: run.user}
    return {}


def _render_payload_run(run) -> tuple[str, str, dict]:
    from sessions.models import RunStatus

    is_schedule = _is_schedule_run(run)
    ok = run.status == RunStatus.SUCCESSFUL
    repo = run.repo_id
    name = run.session.scheduled_job.name if is_schedule else ""
    owner = str(run.session.scheduled_job.user) if is_schedule else ""

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
        "status": run.status,
        "status_label": run.get_status_display(),
        "is_successful": ok,
        "trigger_label": run.get_trigger_type_display(),
        "trigger_name": name,
        "trigger_owner": owner,
        "repo_id": repo,
        "duration_seconds": run.duration,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "total_tokens": run.total_tokens,
        "cost_usd": float(run.cost_usd) if run.cost_usd is not None else None,
    }
    return subject, body, context


def _rollup_exists_run(recipient, batch_id) -> bool:
    from notifications.models import Notification

    return Notification.objects.filter(
        recipient=recipient,
        source_type="sessions.Batch",
        source_id=str(batch_id),
        event_type=EventType.JOB_BATCH_FINISHED,
    ).exists()


def _handle_batch_completion_run(run, siblings, total: int) -> None:
    """Emit a single rollup notification when every sibling in a Run batch is terminal."""
    from sessions.models import RunStatus

    agg = siblings.aggregate(
        terminal=Count("id", filter=Q(status__in=RunStatus.terminal())),
        successful=Count("id", filter=Q(status=RunStatus.SUCCESSFUL)),
        total_input_tokens=Sum("input_tokens"),
        total_output_tokens=Sum("output_tokens"),
        total_total_tokens=Sum("total_tokens"),
        total_cost_usd=Sum("cost_usd"),
    )
    if agg["terminal"] < total:
        return

    recipients = _resolve_recipients_run(run)
    if not recipients:
        logger.warning(
            "Run batch %s completed with no resolvable recipients (run_pk=%s, total=%d)", run.batch_id, run.pk, total
        )
        return

    successful = agg["successful"]
    failed = total - successful
    agg_status = RunStatus.SUCCESSFUL if failed == 0 else RunStatus.FAILED

    rows = list(siblings.values_list("repo_id", "started_at", "finished_at", "status"))

    effective = run.effective_notify_on
    channels = [cls.channel_type for cls in enabled_channels()] if _status_matches_run(effective, agg_status) else []

    usage = {
        "input_tokens": agg["total_input_tokens"],
        "output_tokens": agg["total_output_tokens"],
        "total_tokens": agg["total_total_tokens"],
        "cost_usd": float(agg["total_cost_usd"]) if agg["total_cost_usd"] is not None else None,
    }
    subject, body, context = _render_batch_payload_run(run, rows, total, successful, failed, agg_status, usage)
    link_url = f"{reverse('session_list')}?batch={run.batch_id}"

    for recipient in recipients.values():
        try:
            notify(
                recipient=recipient,
                event_type=EventType.JOB_BATCH_FINISHED,
                source_type="sessions.Batch",
                source_id=str(run.batch_id),
                subject=subject,
                body=body,
                link_url=link_url,
                channels=channels,
                context=context,
            )
        except IntegrityError:
            if _rollup_exists_run(recipient, run.batch_id):
                logger.debug(
                    "Run batch rollup already exists for batch_id=%s recipient_pk=%s",
                    run.batch_id,
                    getattr(recipient, "pk", None),
                )
            else:
                logger.exception(
                    "Unexpected IntegrityError creating run batch notification for batch_id=%s recipient pk=%s",
                    run.batch_id,
                    getattr(recipient, "pk", None),
                )
        except Exception:
            logger.exception(
                "Failed to create run batch notification for batch_id=%s recipient pk=%s",
                run.batch_id,
                getattr(recipient, "pk", None),
            )


def _render_batch_payload_run(
    run, rows: list[tuple], total: int, successful: int, failed: int, agg_status: str, usage: dict
) -> tuple[str, str, dict]:
    from sessions.models import RunStatus

    is_schedule = _is_schedule_run(run)
    ok = failed == 0
    repo_ids = sorted({repo for repo, _start, _end, _status in rows if repo})
    repo_results = [{"repo": repo, "ok": status == RunStatus.SUCCESSFUL} for repo, _start, _end, status in rows if repo]
    name = run.session.scheduled_job.name if is_schedule else ""
    owner = str(run.session.scheduled_job.user) if is_schedule else ""

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
        "status_label": str(agg_status),
        "is_successful": ok,
        "trigger_label": run.get_trigger_type_display(),
        "trigger_name": name,
        "trigger_owner": owner,
        "repo_id": repo_ids[0] if len(repo_ids) == 1 else "",
        "repo_ids": repo_ids,
        "repo_results": repo_results,
        "total": total,
        "successful_count": successful,
        "failed_count": failed,
        "duration_seconds": _batch_duration(rows),
        "batch_id": str(run.batch_id),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "total_tokens": usage["total_tokens"],
        "cost_usd": usage["cost_usd"],
    }
    return subject, body, context


@receiver(run_finished, dispatch_uid="notifications.on_run_finished")
def on_run_finished(sender, run, **kwargs) -> None:
    """Notify recipients when a Run transitions to a terminal status.

    Chat-triggered runs are excluded: those are interactive sessions and should
    not generate bell/email notifications (preserves today's behaviour for chat).
    Webhook-triggered runs are excluded to avoid noise on automated operations.
    """
    from sessions.models import Run, SessionOrigin

    try:
        if run.trigger_type == SessionOrigin.CHAT:
            return
        if run.trigger_type in EXCLUDED_RUN_TRIGGERS:
            return

        if run.batch_id is not None:
            siblings = Run.objects.by_batch(run.batch_id)
            total = siblings.count()
            if total > 1:
                _handle_batch_completion_run(run, siblings, total)
                return

        recipients = _resolve_recipients_run(run)
        if not recipients:
            return

        effective = run.effective_notify_on
        channels = (
            [cls.channel_type for cls in enabled_channels()] if _status_matches_run(effective, run.status) else []
        )

        subject, body, context = _render_payload_run(run)
        link_url = reverse("session_detail", kwargs={"thread_id": run.session_id})
        event_type = EventType.SCHEDULE_FINISHED if _is_schedule_run(run) else EventType.JOB_FINISHED

        for recipient in recipients.values():
            try:
                notify(
                    recipient=recipient,
                    event_type=event_type,
                    source_type="sessions.Run",
                    source_id=str(run.pk),
                    subject=subject,
                    body=body,
                    link_url=link_url,
                    channels=channels,
                    context=context,
                )
            except Exception:
                logger.exception(
                    "Failed to create notification for run %s, recipient pk=%s", run.pk, getattr(recipient, "pk", None)
                )
    except Exception:
        logger.exception("on_run_finished: unexpected error for run=%s", getattr(run, "pk", run))


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
