from __future__ import annotations

from typing import TYPE_CHECKING

from django.urls import reverse
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext as _

from notifications.channels.registry import enabled_channel_types
from notifications.policy import notification_source_for_schedule_failure
from notifications.run_notifiers import deliver_to_recipients
from schedules.models import DispatchError, Frequency

if TYPE_CHECKING:
    from schedules.models import ScheduledJob


def _render_payload(schedule: ScheduledJob) -> tuple[str, str, dict]:
    repo_ids = schedule.dispatch_error_repo_ids or [r["repo_id"] for r in schedule.repos]

    subject = _('Schedule "{name}" can\'t run').format(name=schedule.name)

    if schedule.dispatch_error == DispatchError.REPO_ACCESS_DENIED:
        cause = _("Its last scheduled run was skipped because you don't have write access to {repos}.").format(
            repos=", ".join(repo_ids)
        )
    else:
        cause = _(
            "Its last scheduled run couldn't start because of an unexpected error. "
            "The error was logged for the DAIV operators."
        )

    if schedule.frequency == Frequency.ONCE:
        outlook = _("It was a one-off, so it won't run again. Once this is fixed, duplicate the schedule to run it.")
    else:
        outlook = _(
            "DAIV keeps trying at each scheduled time. You won't get this message again unless the schedule "
            "recovers and then fails again."
        )

    body = f"{cause} {outlook}"
    context = {
        "status_tone": "failure",
        "status_label": _("Can't run"),
        "schedule_name": schedule.name,
        "repo_ids": repo_ids,
        "reason": schedule.dispatch_error_message,
        "last_run": date_format(timezone.localtime(schedule.last_run_at), "M j, H:i")
        if schedule.last_run_at
        else _("Never"),
        "summary": body,
    }
    return subject, body, context


def emit_schedule_dispatch_failed(schedule: ScheduledJob) -> None:
    """Tell the schedule owner that its dispatches started failing.

    Only the owner is told: subscribers can't fix the owner's repository access. ``muted`` is not
    consulted, since it silences run results and this notice is about the schedule not running at all.
    """
    subject, body, context = _render_payload(schedule)
    source_type, source_id, event_type = notification_source_for_schedule_failure(schedule)

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
