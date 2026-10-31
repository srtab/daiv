from __future__ import annotations

import logging
from datetime import timedelta

from django.utils import timezone

from notifications.choices import EnvelopeStatus, EventType

logger = logging.getLogger("daiv.notifications")

# Opaque namespacing keys for the per-run / per-batch dedup, stored on Notification.source_type and
# pinned by the unique constraint. Spelled once here so the emit and the re-drive can't drift.
SOURCE_RUN = "sessions.Run"
SOURCE_BATCH = "sessions.Batch"
SOURCE_SESSION = "sessions.Session"
SOURCE_SCHEDULE = "schedules.ScheduledJob"

# Recency floor so a first deploy of universal classification never sweeps every historical webhook/job
# run into paid classification; the notification relevance window reuses this same value.
RECLASSIFY_MAX_AGE = timedelta(hours=24)

NOTIFY_WORTHY_STATUSES = frozenset({
    EnvelopeStatus.FOUND_ISSUES,
    EnvelopeStatus.NEEDS_ATTENTION,
    EnvelopeStatus.NEEDS_INPUT,
    EnvelopeStatus.FAILED,
})

STATUS_TONES = {
    EnvelopeStatus.FAILED: "failure",
    EnvelopeStatus.FOUND_ISSUES: "warning",
    EnvelopeStatus.NEEDS_ATTENTION: "warning",
    EnvelopeStatus.NEEDS_INPUT: "warning",
    EnvelopeStatus.ALL_CLEAR: "success",
}


def is_schedule_run(run) -> bool:
    """True when ``run`` belongs to a session with a still-loadable ScheduledJob."""
    session = run.session if run.session_id else None
    return session is not None and session.scheduled_job_id is not None and session.scheduled_job is not None


def notify_worthy(status: str) -> bool:
    """Whether an envelope status is worth a look; ``all-clear`` is silent (it lives in the Feed).
    Per-run delivery decisions go through ``notifies()``, which adds the run-level exceptions."""
    return status in NOTIFY_WORTHY_STATUSES


def notifies(run, status: str) -> bool:
    """Whether this run's outcome notifies. A webhook question is already posted on the forge, mentioning the asker."""
    from sessions.models import SessionOrigin

    if status == EnvelopeStatus.NEEDS_INPUT and run.trigger_type in SessionOrigin.webhooks():
        return False
    return notify_worthy(status)


def status_severity(status: str) -> int:
    """Rank a classified run for a list that is then capped.

    An unranked status sorts *first* — under a cap, being dropped is the one outcome a reader
    cannot recover from.
    """
    try:
        return EnvelopeStatus.worst_first().index(status)
    except ValueError:
        logger.error("status_severity: unranked envelope status %r, sorting it first", status)
        return -1


def within_relevance_window(finished_at) -> bool:
    """Notify only for runs that finished recently.

    The window reuses ``RECLASSIFY_MAX_AGE`` (one shared knob): inside it we prefer late delivery over
    dropping, so an outage-delayed but genuinely-recent run still notifies.
    """
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


def notification_source_for_watch(session, sha: str) -> tuple[str, str, EventType]:
    """The key a pipeline-watch give-up is deduped on: one per commit DAIV pushed, so retrying the same failing
    pipeline does not notify again, while a later commit that also runs out of attempts does."""
    return SOURCE_SESSION, f"{session.thread_id}:{sha}", EventType.PIPELINE_WATCH_EXHAUSTED


def notification_source_for_schedule_failure(schedule) -> tuple[str, str, EventType]:
    """The key a schedule's dispatch failure is deduped on: one notice per failing streak."""
    return SOURCE_SCHEDULE, f"{schedule.pk}:{schedule.failing_since.isoformat()}", EventType.SCHEDULE_DISPATCH_FAILED


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
    """A single run's tone, shared by the email pill and the RocketChat attachment so the two
    channels agree. An untoned status renders as a failure, never green."""
    if (tone := STATUS_TONES.get(status)) is None:
        logger.error("envelope_tone: untoned envelope status %r, rendering it as a failure", status)
        return "failure"
    return tone
