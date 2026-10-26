"""What a notification row says happened, derived from what each notifier already stores.

A per-run notification is keyed on its run (``source_type == SOURCE_RUN``), and that run's
``RunEnvelope`` is written once and never updated, so its status is the outcome the notification
was sent for. A batch rollup stores its per-status counts; the watch and dispatch notices have a
single outcome each.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from django.utils.translation import gettext_lazy as _

from notifications.choices import EnvelopeStatus, EventType
from notifications.models import RunEnvelope
from notifications.policy import SOURCE_RUN

if TYPE_CHECKING:
    from collections.abc import Iterable

    from notifications.models import Notification


class Outcome(NamedTuple):
    key: str
    label: str
    icon: str
    tone: str  # clear / found / attn / fail — the DESIGN.md status token it is painted with


_ENVELOPE_LOOKS: dict[EnvelopeStatus, tuple[str, str]] = {
    EnvelopeStatus.FAILED: ("exclamation-circle", "fail"),
    EnvelopeStatus.NEEDS_INPUT: ("chat-bubble", "found"),
    EnvelopeStatus.FOUND_ISSUES: ("exclamation-triangle", "found"),
    EnvelopeStatus.NEEDS_ATTENTION: ("magnifying-glass", "attn"),
    EnvelopeStatus.ALL_CLEAR: ("check-circle", "clear"),
}
ENVELOPE_OUTCOMES: dict[str, Outcome] = {
    status: Outcome(status, status.label, icon, tone) for status, (icon, tone) in _ENVELOPE_LOOKS.items()
}
CI_STILL_FAILING = Outcome("ci-still-failing", _("CI still failing"), "arrow-path", "fail")
CANT_RUN = Outcome("cant-run", _("Can't run"), "pause", "fail")

_BATCH_COUNT_KEYS = {
    EnvelopeStatus.FAILED: "failed_count",
    EnvelopeStatus.NEEDS_INPUT: "needs_input_count",
    EnvelopeStatus.FOUND_ISSUES: "found_count",
    EnvelopeStatus.NEEDS_ATTENTION: "needs_attention_count",
    EnvelopeStatus.ALL_CLEAR: "all_clear_count",
}
_TONE_STATUSES = {
    "failure": EnvelopeStatus.FAILED,
    "warning": EnvelopeStatus.NEEDS_ATTENTION,
    "success": EnvelopeStatus.ALL_CLEAR,
}


def _batch_status(context: dict) -> str | None:
    """The worst status any run in the batch reached, so a rollup never hides its failure."""
    for status in EnvelopeStatus.worst_first():
        if context.get(_BATCH_COUNT_KEYS[status]):
            return status
    return None


def outcome_for(notification: Notification, envelope_status: str | None = None) -> Outcome:
    """The outcome to show on ``notification``'s row; ``envelope_status`` is its run's, when per-run."""
    if notification.event_type == EventType.PIPELINE_WATCH_EXHAUSTED:
        return CI_STILL_FAILING
    if notification.event_type == EventType.SCHEDULE_DISPATCH_FAILED:
        return CANT_RUN

    context = notification.context or {}
    status = envelope_status
    if notification.event_type == EventType.JOB_BATCH_FINISHED:
        status = _batch_status(context)
    # A run deleted since (its envelope cascades away) still has the tone it was sent with.
    if status is None:
        status = _TONE_STATUSES.get(context.get("status_tone"), EnvelopeStatus.NEEDS_ATTENTION)
    return ENVELOPE_OUTCOMES.get(status, ENVELOPE_OUTCOMES[EnvelopeStatus.NEEDS_ATTENTION])


def attach_outcomes(notifications: Iterable[Notification]) -> None:
    """Set ``.outcome`` on each notification, reading every per-run envelope in one query."""
    notifications = list(notifications)
    run_ids = {n.source_id for n in notifications if n.source_type == SOURCE_RUN and n.source_id}
    statuses = {
        str(run_id): status
        for run_id, status in RunEnvelope.objects.filter(run_id__in=run_ids).values_list("run_id", "status")
    }
    for notification in notifications:
        envelope_status = statuses.get(notification.source_id) if notification.source_type == SOURCE_RUN else None
        notification.outcome = outcome_for(notification, envelope_status)
