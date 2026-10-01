from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from sessions.models import SessionOrigin

from notifications.channels.registry import enabled_channel_types
from notifications.policy import notification_source_for_schedule_failure
from notifications.run_notifiers import deliver_to_recipients, notification_exists
from schedules.models import ScheduledJob


def _render_payload(schedule: ScheduledJob) -> tuple[str, str, dict]:
    repo_ids = schedule.dispatch_error_repo_ids or [r["repo_id"] for r in schedule.repos]

    subject = _('Schedule "{name}" can\'t run').format(name=schedule.name)

    if schedule.is_access_denied:
        cause = _(
            "A scheduled run was skipped because DAIV couldn't confirm that you have write access to {repos}. "
            "If you do, ask a DAIV administrator to check that your account is linked and your repository "
            "access is up to date."
        ).format(repos=", ".join(repo_ids))
    else:
        cause = _(
            "A scheduled run couldn't start because of an unexpected error. "
            "The error was logged for the DAIV operators."
        )

    parts = [cause, schedule.dispatch_failure_outlook]
    if schedule.is_enabled:
        parts.append(_("You won't get this message again unless the schedule recovers and then fails again."))
    body = " ".join(parts)
    context = {
        "status_tone": "failure",
        "status_label": _("Can't run"),
        "trigger_label": SessionOrigin.SCHEDULE.label,
        "trigger_name": schedule.name,
        "repo_id": repo_ids[0] if len(repo_ids) == 1 else "",
        "repo_ids": repo_ids,
        "reason": schedule.dispatch_error_message,
        "last_run": date_format(timezone.localtime(schedule.last_run_at), "M j, H:i")
        if schedule.last_run_at
        else _("Never"),
        "summary": body,
    }
    return subject, body, context


def emit_schedule_dispatch_failed(schedule_pk: int) -> None:
    """Tell the schedule owner that its dispatches are failing, once per failing streak.

    Called on every failed dispatch: the streak-keyed dedupe makes repeats no-ops, so a notice lost to
    an error is sent on the next failure. Owner only, and sent even when ``muted``: mute silences run
    results, not a schedule that cannot run.
    """
    schedule = ScheduledJob.objects.select_related("user").filter(pk=schedule_pk).first()
    if schedule is None or schedule.failing_since is None:
        return

    source_type, source_id, event_type = notification_source_for_schedule_failure(schedule)
    if notification_exists(schedule.user, source_type, source_id, event_type):
        return

    subject, body, context = _render_payload(schedule)

    deliver_to_recipients(
        [schedule.user],
        source_type=source_type,
        source_id=source_id,
        event_type=event_type,
        subject=subject,
        body=body,
        link_url=reverse("schedule_update", args=[schedule.pk]),
        channels=enabled_channel_types(),
        context=context,
    )
