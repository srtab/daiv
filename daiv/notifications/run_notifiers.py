from __future__ import annotations

import logging
from typing import TYPE_CHECKING, NamedTuple

from django.db import IntegrityError
from django.db.models import Count, Q, Sum
from django.urls import reverse
from django.utils.translation import gettext as _

from notifications.channels.registry import enabled_channel_types
from notifications.policy import (
    batch_status_tone,
    envelope_tone,
    is_schedule_run,
    notification_source_for_run,
    notify_worthy,
    within_relevance_window,
)
from notifications.services import notify

if TYPE_CHECKING:
    from datetime import datetime

logger = logging.getLogger("daiv.notifications")


class BatchRow(NamedTuple):
    """One sibling's row in a batch rollup — named so the positional values_list can't drift."""

    repo: str
    started_at: datetime | None
    finished_at: datetime | None
    status: str


def _summarize_repos(repo_ids: list[str], limit: int = 3) -> str:
    if not repo_ids:
        return ""
    if len(repo_ids) <= limit:
        return ", ".join(repo_ids)
    head = ", ".join(repo_ids[:limit])
    return _("%(repos)s and %(remaining)d more") % {"repos": head, "remaining": len(repo_ids) - limit}


def _batch_duration(rows: list[BatchRow]) -> float | None:
    """Wall-clock span from earliest start to latest finish across the batch."""
    spans = [(r.started_at, r.finished_at) for r in rows if r.started_at and r.finished_at]
    if not spans:
        return None
    return (max(end for _, end in spans) - min(start for start, _ in spans)).total_seconds()


def resolve_recipients(run) -> dict[int, object]:
    if is_schedule_run(run):
        schedule = run.session.scheduled_job
        recipients: dict[int, object] = {schedule.user_id: schedule.user}
        for sub in schedule.subscribers.all():
            recipients.setdefault(sub.pk, sub)
        return recipients
    if run.user is not None:
        return {run.user.pk: run.user}
    return {}


def _render_payload(run, envelope) -> tuple[str, str, dict]:
    from sessions.models import EnvelopeStatus, RunStatus

    is_schedule = is_schedule_run(run)
    repo = run.repo_id
    name = run.session.scheduled_job.name if is_schedule else ""
    owner = str(run.session.scheduled_job.user) if is_schedule else ""
    status = envelope.status
    count = envelope.count

    # The caller gates on notify_worthy() first, so status is found-issues / needs-attention / failed;
    # an unmatched status would leave subject unbound, so the else raises instead of guessing.
    if is_schedule:
        params = {"name": name, "owner": owner, "repo": repo, "count": count}
        if status == EnvelopeStatus.FOUND_ISSUES:
            subject = _("'%(name)s' found %(count)d issue(s) on %(repo)s — %(owner)s") % params
        elif status == EnvelopeStatus.NEEDS_ATTENTION:
            subject = _("'%(name)s' needs attention on %(repo)s — %(owner)s") % params
        elif status == EnvelopeStatus.FAILED:
            subject = _("'%(name)s' failed on %(repo)s — %(owner)s") % params
        else:
            raise ValueError(f"unexpected notify-worthy envelope status {status!r}")
    else:
        params = {"repo": repo, "count": count}
        if status == EnvelopeStatus.FOUND_ISSUES:
            subject = _("Agent run on %(repo)s found %(count)d issue(s)") % params
        elif status == EnvelopeStatus.NEEDS_ATTENTION:
            subject = _("Agent run on %(repo)s needs attention") % params
        elif status == EnvelopeStatus.FAILED:
            subject = _("Agent run on %(repo)s failed") % params
        else:
            raise ValueError(f"unexpected notify-worthy envelope status {status!r}")

    body = envelope.summary or subject

    context = {
        # Pill/attachment tone is driven by the envelope (what the run found), not the run's own
        # success — a found-issues run succeeds yet must not render green. is_successful stays for
        # legacy renderers as a fallback when status_tone is absent.
        "status_tone": envelope_tone(status),
        "status": run.status,
        "status_label": envelope.get_status_display(),
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


def _render_batch_payload(
    run, rows: list[BatchRow], total: int, agg: dict, notable: int, usage: dict
) -> tuple[str, str, dict]:
    is_schedule = is_schedule_run(run)
    repo_ids = sorted({r.repo for r in rows if r.repo})
    name = run.session.scheduled_job.name if is_schedule else ""
    owner = str(run.session.scheduled_job.user) if is_schedule else ""

    if is_schedule:
        params = {"name": name, "owner": owner, "notable": notable, "total": total}
        subject = _("'%(name)s' batch: %(notable)d/%(total)d need a look — %(owner)s") % params
    else:
        repo_summary = _summarize_repos(repo_ids)
        params = {"notable": notable, "total": total, "repos": repo_summary}
        # A webhook/API batch has no name or owner to identify it, so name the repos in the subject.
        if repo_summary:
            subject = _("Agent run batch: %(notable)d/%(total)d need a look — %(repos)s") % params
        else:
            subject = _("Agent run batch: %(notable)d/%(total)d need a look") % params

    body = _(
        "%(found)d found issues, %(needs)d need attention, %(failed)d failed, %(clear)d all-clear (of %(total)d runs)."
    ) % {"found": agg["found"], "needs": agg["needs"], "failed": agg["failed"], "clear": agg["clear"], "total": total}

    context = {
        "found_count": agg["found"],
        "needs_attention_count": agg["needs"],
        "failed_count": agg["failed"],
        "all_clear_count": agg["clear"],
        "notable_count": notable,
        "total": total,
        "status_label": _("Needs attention"),
        "status_tone": batch_status_tone(notable, total),
        "trigger_label": run.get_trigger_type_display(),
        "trigger_name": name,
        "trigger_owner": owner,
        "repo_id": repo_ids[0] if len(repo_ids) == 1 else "",
        "repo_ids": repo_ids,
        "duration_seconds": _batch_duration(rows),
        "batch_id": str(run.batch_id),
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "total_tokens": usage["total_tokens"],
        "cost_usd": usage["cost_usd"],
    }
    return subject, body, context


def _notification_exists(recipient, source_type: str, source_id: str, event_type) -> bool:
    from notifications.models import Notification

    return Notification.objects.filter(
        recipient=recipient, source_type=source_type, source_id=source_id, event_type=event_type
    ).exists()


def deliver_to_recipients(
    recipients, *, source_type: str, source_id: str, event_type, subject, body, link_url, channels, context
) -> None:
    """Create one notification per recipient, keyed on (source_type, source_id, event_type).

    At-least-once-then-deduped: the unique constraint makes a repeat insert raise IntegrityError. A row
    already present under the same key is the benign dedup (raced or re-driven); its absence is a real
    bug. The existence check reuses the insert's key so the two can't diverge. One recipient's failure
    never blocks the others.
    """
    for recipient in recipients:
        pk = getattr(recipient, "pk", None)
        try:
            notify(
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
        except IntegrityError:
            key = f"{source_type}={source_id}"
            if _notification_exists(recipient, source_type, source_id, event_type):
                logger.debug("Notification already exists for %s recipient_pk=%s (raced/re-driven)", key, pk)
            else:
                logger.exception("Unexpected IntegrityError creating notification for %s recipient pk=%s", key, pk)
        except Exception:
            logger.exception("Failed to create notification for %s=%s recipient pk=%s", source_type, source_id, pk)


def _handle_batch_completion(run, siblings, total: int) -> None:
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

    recipients = resolve_recipients(run)
    if not recipients:
        logger.warning(
            "Run batch %s completed with no resolvable recipients (run_pk=%s, total=%d)", run.batch_id, run.pk, total
        )
        return

    channels = enabled_channel_types()
    rows = [BatchRow(*row) for row in siblings.values_list("repo_id", "started_at", "finished_at", "envelope__status")]
    usage = {
        "input_tokens": agg["total_input_tokens"],
        "output_tokens": agg["total_output_tokens"],
        "total_tokens": agg["total_total_tokens"],
        "cost_usd": float(agg["total_cost_usd"]) if agg["total_cost_usd"] is not None else None,
    }
    subject, body, context = _render_batch_payload(run, rows, total, agg, notable, usage)
    link_url = f"{reverse('session_list')}?batch={run.batch_id}"
    source_type, source_id, event_type = notification_source_for_run(run, total)

    deliver_to_recipients(
        recipients.values(),
        source_type=source_type,
        source_id=source_id,
        event_type=event_type,
        subject=subject,
        body=body,
        link_url=link_url,
        channels=channels,
        context=context,
    )


def emit_run_notification(run, envelope) -> None:
    """Notify recipients when a Run is classified, driven by the envelope (not raw status).

    Chat is never classified, so no chat special-case is needed. all-clear is silent; found-issues /
    needs-attention / failed notify unless muted, within the relevance window. Delivery is
    at-least-once-then-deduped (the per-run unique constraint + the re-drive backstop).
    """
    from sessions.models import Run

    # Relevance window first: a coverage-widening deploy must not retro-blast pre-feature runs.
    if not within_relevance_window(run.finished_at):
        return

    if run.batch_id is not None:
        siblings = Run.objects.by_batch(run.batch_id)
        total = siblings.count()
        if total > 1:
            _handle_batch_completion(run, siblings, total)
            return

    if not notify_worthy(envelope.status) or run.effective_muted:
        return

    recipients = resolve_recipients(run)
    if not recipients:
        # Matches the batch path: a notify-worthy run with no resolvable recipient (e.g. a webhook run
        # whose external actor has no DAIV account) is a dropped notification worth surfacing to ops.
        logger.warning(
            "Notify-worthy run %s (%s, envelope=%s) has no resolvable recipient; nothing delivered",
            run.pk,
            run.trigger_type,
            envelope.status,
        )
        return

    channels = enabled_channel_types()
    subject, body, context = _render_payload(run, envelope)
    link_url = reverse("session_detail", kwargs={"thread_id": run.session_id})
    source_type, source_id, event_type = notification_source_for_run(run, total=1)

    deliver_to_recipients(
        recipients.values(),
        source_type=source_type,
        source_id=source_id,
        event_type=event_type,
        subject=subject,
        body=body,
        link_url=link_url,
        channels=channels,
        context=context,
    )
