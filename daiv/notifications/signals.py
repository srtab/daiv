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

from sessions.signals import run_classified

from notifications.channels.registry import enabled_channels
from notifications.choices import ChannelType, EventType
from notifications.models import UserChannelBinding
from notifications.services import notify

logger = logging.getLogger("daiv.notifications")


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


def _render_payload_run(run, envelope) -> tuple[str, str, dict]:
    from sessions.models import EnvelopeStatus, RunStatus

    is_schedule = _is_schedule_run(run)
    repo = run.repo_id
    name = run.session.scheduled_job.name if is_schedule else ""
    owner = str(run.session.scheduled_job.user) if is_schedule else ""
    status = envelope.status
    count = envelope.count

    if is_schedule:
        params = {"name": name, "owner": owner, "repo": repo, "count": count}
        if status == EnvelopeStatus.FOUND_ISSUES:
            subject = _("'%(name)s' found %(count)d issue(s) on %(repo)s — %(owner)s") % params
        elif status == EnvelopeStatus.NEEDS_ATTENTION:
            subject = _("'%(name)s' needs attention on %(repo)s — %(owner)s") % params
        else:  # FAILED
            subject = _("'%(name)s' failed on %(repo)s — %(owner)s") % params
    else:
        params = {"repo": repo, "count": count}
        if status == EnvelopeStatus.FOUND_ISSUES:
            subject = _("Agent run on %(repo)s found %(count)d issue(s)") % params
        elif status == EnvelopeStatus.NEEDS_ATTENTION:
            subject = _("Agent run on %(repo)s needs attention") % params
        else:  # FAILED
            subject = _("Agent run on %(repo)s failed") % params

    body = envelope.summary or subject

    context = {
        "envelope_status": status,
        "envelope_status_label": envelope.get_status_display(),
        "envelope_summary": envelope.summary,
        "envelope_count": count,
        # Existing keys kept so channel renderers (email / rocketchat) do not break.
        "status": run.status,
        "status_label": run.get_status_display(),
        "is_successful": run.status == RunStatus.SUCCESSFUL,
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


def _per_run_notification_exists(recipient, run, event_type) -> bool:
    from notifications.models import Notification

    return Notification.objects.filter(
        recipient=recipient, source_type="sessions.Run", source_id=str(run.pk), event_type=event_type
    ).exists()


def _handle_batch_completion_run(run, siblings, total: int) -> None:
    """Emit one rollup once every sibling in the batch is classified (has an envelope)."""
    from sessions.models import EnvelopeStatus

    classified = siblings.filter(envelope__isnull=False).count()
    if classified < total:
        return

    agg = siblings.aggregate(
        found=Count("id", filter=Q(envelope__status=EnvelopeStatus.FOUND_ISSUES)),
        needs=Count("id", filter=Q(envelope__status=EnvelopeStatus.NEEDS_ATTENTION)),
        failed=Count("id", filter=Q(envelope__status=EnvelopeStatus.FAILED)),
        clear=Count("id", filter=Q(envelope__status=EnvelopeStatus.ALL_CLEAR)),
        total_input_tokens=Sum("input_tokens"),
        total_output_tokens=Sum("output_tokens"),
        total_total_tokens=Sum("total_tokens"),
        total_cost_usd=Sum("cost_usd"),
    )
    notable = agg["found"] + agg["needs"] + agg["failed"]
    if notable == 0 or run.effective_muted:
        return

    recipients = _resolve_recipients_run(run)
    if not recipients:
        logger.warning(
            "Run batch %s completed with no resolvable recipients (run_pk=%s, total=%d)", run.batch_id, run.pk, total
        )
        return

    channels = [cls.channel_type for cls in enabled_channels()]
    rows = list(siblings.values_list("repo_id", "started_at", "finished_at", "envelope__status"))
    usage = {
        "input_tokens": agg["total_input_tokens"],
        "output_tokens": agg["total_output_tokens"],
        "total_tokens": agg["total_total_tokens"],
        "cost_usd": float(agg["total_cost_usd"]) if agg["total_cost_usd"] is not None else None,
    }
    subject, body, context = _render_batch_payload_run(run, rows, total, agg, notable, usage)
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
    run, rows: list[tuple], total: int, agg: dict, notable: int, usage: dict
) -> tuple[str, str, dict]:
    is_schedule = _is_schedule_run(run)
    repo_ids = sorted({repo for repo, _start, _end, _status in rows if repo})
    name = run.session.scheduled_job.name if is_schedule else ""
    owner = str(run.session.scheduled_job.user) if is_schedule else ""
    breakdown = {
        "found": agg["found"],
        "needs": agg["needs"],
        "failed": agg["failed"],
        "clear": agg["clear"],
        "notable": notable,
        "total": total,
    }

    if is_schedule:
        params = {"name": name, "owner": owner, "notable": notable, "total": total}
        subject = _("'%(name)s' batch: %(notable)d/%(total)d need a look — %(owner)s") % params
    else:
        repo_summary = _summarize_repos(repo_ids)
        params = {"notable": notable, "total": total, "repos": repo_summary}
        subject = _("Agent run batch: %(notable)d/%(total)d need a look") % params

    body_tmpl = _(
        "%(found)d found issues, %(needs)d need attention, %(failed)d failed, %(clear)d all-clear (of %(total)d runs)."
    )
    body = body_tmpl % breakdown

    context = {
        "found_count": agg["found"],
        "needs_attention_count": agg["needs"],
        "failed_count": agg["failed"],
        "all_clear_count": agg["clear"],
        "notable_count": notable,
        "total": total,
        "trigger_label": run.get_trigger_type_display(),
        "trigger_name": name,
        "trigger_owner": owner,
        "repo_id": repo_ids[0] if len(repo_ids) == 1 else "",
        "repo_ids": repo_ids,
        "duration_seconds": _batch_duration([(r, s, e, st) for r, s, e, st in rows]),
        "batch_id": str(run.batch_id),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "total_tokens": usage["total_tokens"],
        "cost_usd": usage["cost_usd"],
    }
    return subject, body, context


def _notify_worthy_statuses() -> frozenset[str]:
    from sessions.models import EnvelopeStatus

    return frozenset({EnvelopeStatus.FOUND_ISSUES, EnvelopeStatus.NEEDS_ATTENTION, EnvelopeStatus.FAILED})


def notify_worthy(status: str) -> bool:
    """The single notification predicate: notify only when the run produced something to look at.
    ``all-clear`` is silent (it lives in the Feed)."""
    return status in _notify_worthy_statuses()


def _within_relevance_window(finished_at) -> bool:
    """Notify only for runs that finished recently and after the coverage-widening cutoff.

    NOTIFY_MAX_AGE == RECLASSIFY_MAX_AGE (one shared knob): inside the window we prefer late delivery
    over dropping, so an outage-delayed but genuinely-recent run still notifies.
    """
    from sessions.tasks import RECLASSIFY_MAX_AGE

    from notifications.conf import settings as notif_settings

    if finished_at is None:
        return False
    now = timezone.now()
    if finished_at < now - RECLASSIFY_MAX_AGE:
        return False
    not_before = notif_settings.NOTIFY_NOT_BEFORE
    if not_before is not None:
        if timezone.is_naive(not_before):
            not_before = timezone.make_aware(not_before)
        if finished_at < not_before:
            return False
    return True


@receiver(run_classified, dispatch_uid="notifications.on_run_classified")
def on_run_classified(sender, run, envelope, **kwargs) -> None:
    """Notify recipients when a Run is classified, driven by the envelope (not raw status).

    Chat is never classified, so no chat special-case is needed. all-clear is silent; found-issues /
    needs-attention / failed notify unless muted, within the relevance window. Delivery is
    at-least-once-then-deduped (the per-run unique constraint + the re-drive backstop).
    """
    from sessions.models import Run

    try:
        # Relevance window first: a coverage-widening deploy must not retro-blast pre-feature runs,
        # and a run with no finished_at (not yet terminal in-memory) is never notified.
        if not _within_relevance_window(run.finished_at):
            return

        if run.batch_id is not None:
            siblings = Run.objects.by_batch(run.batch_id)
            total = siblings.count()
            if total > 1:
                _handle_batch_completion_run(run, siblings, total)
                return

        if not notify_worthy(envelope.status) or run.effective_muted:
            return

        recipients = _resolve_recipients_run(run)
        if not recipients:
            return

        channels = [cls.channel_type for cls in enabled_channels()]
        subject, body, context = _render_payload_run(run, envelope)
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
            except IntegrityError:
                if _per_run_notification_exists(recipient, run, event_type):
                    logger.debug(
                        "Per-run notification already exists for run=%s recipient_pk=%s (raced/re-driven)",
                        run.pk,
                        getattr(recipient, "pk", None),
                    )
                else:
                    logger.exception(
                        "Unexpected IntegrityError creating notification for run=%s recipient pk=%s",
                        run.pk,
                        getattr(recipient, "pk", None),
                    )
            except Exception:
                logger.exception(
                    "Failed to create notification for run %s, recipient pk=%s", run.pk, getattr(recipient, "pk", None)
                )
    except Exception:
        logger.exception("on_run_classified: unexpected error for run=%s", getattr(run, "pk", run))


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
