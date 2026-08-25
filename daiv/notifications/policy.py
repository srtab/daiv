from __future__ import annotations

from django.utils import timezone

from notifications.choices import EventType

# Opaque namespacing keys for the per-run / per-batch dedup, stored on Notification.source_type and
# pinned by the unique constraint. Spelled once here so the emit and the re-drive can't drift.
SOURCE_RUN = "sessions.Run"
SOURCE_BATCH = "sessions.Batch"


def is_schedule_run(run) -> bool:
    """True when ``run`` belongs to a session with a still-loadable ScheduledJob."""
    session = run.session if run.session_id else None
    return session is not None and session.scheduled_job_id is not None and session.scheduled_job is not None


def notify_worthy_statuses() -> frozenset[str]:
    from sessions.models import EnvelopeStatus

    return frozenset({EnvelopeStatus.FOUND_ISSUES, EnvelopeStatus.NEEDS_ATTENTION, EnvelopeStatus.FAILED})


def notify_worthy(status: str) -> bool:
    """The single notification predicate: notify only when the run produced something to look at.
    ``all-clear`` is silent (it lives in the Feed)."""
    return status in notify_worthy_statuses()


def status_severity(status: str) -> int:
    """Worst-first rank for a classified run, used to order a *capped* list of them.

    Load-bearing because of the cap: unordered rows let a batch show three needs-attention repos
    and hide the failure. An unrecognised status sorts last.
    """
    from sessions.models import EnvelopeStatus

    order = (
        EnvelopeStatus.FAILED,
        EnvelopeStatus.FOUND_ISSUES,
        EnvelopeStatus.NEEDS_ATTENTION,
        EnvelopeStatus.ALL_CLEAR,
    )
    try:
        return order.index(status)
    except ValueError:
        return len(order)


def within_relevance_window(finished_at) -> bool:
    """Notify only for runs that finished recently.

    The window reuses ``RECLASSIFY_MAX_AGE`` (one shared knob): inside it we prefer late delivery over
    dropping, so an outage-delayed but genuinely-recent run still notifies.
    """
    from sessions.tasks import RECLASSIFY_MAX_AGE

    if finished_at is None:
        return False
    now = timezone.now()
    return finished_at >= now - RECLASSIFY_MAX_AGE


def notification_source_for_run(run, total: int) -> tuple[str, str, EventType]:
    """The (source_type, source_id, event_type) a classified run's notification is keyed on.

    A batch of >1 rolls up to one JOB_BATCH_FINISHED per (recipient, batch); a single-repo submit
    (total == 1, but still carries a batch_id) and webhook runs are per-run. The emit and the re-drive
    both key through this function so their "already delivered?" lookups cannot diverge.
    """
    if run.batch_id is not None and total > 1:
        return SOURCE_BATCH, str(run.batch_id), EventType.JOB_BATCH_FINISHED
    event_type = EventType.SCHEDULE_FINISHED if is_schedule_run(run) else EventType.JOB_FINISHED
    return SOURCE_RUN, str(run.pk), event_type


def batch_status_tone(notable: int, total: int) -> str:
    """Single source of truth for a batch's aggregate tone, shared by the email pill and the
    RocketChat attachment so the two channels can never disagree. ``notable == 0`` is all-clear
    (only reachable defensively — a notified batch always has notable > 0)."""
    if notable == 0:
        return "success"
    if notable == total:
        return "failure"
    return "warning"


def envelope_tone(status: str) -> str:
    """A single run's tone, shared by the email pill and the RocketChat attachment so the two channels
    agree. found-issues / needs-attention are a warning; failed is a failure. all-clear is success
    (only reachable defensively — a notified run is never all-clear)."""
    from sessions.models import EnvelopeStatus

    if status == EnvelopeStatus.FAILED:
        return "failure"
    if status in (EnvelopeStatus.FOUND_ISSUES, EnvelopeStatus.NEEDS_ATTENTION):
        return "warning"
    return "success"
