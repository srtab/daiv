from __future__ import annotations

import logging

from django.utils import timezone

from notifications.choices import EventType

logger = logging.getLogger("daiv.notifications")

# Opaque namespacing keys for the per-run / per-batch dedup, stored on Notification.source_type and
# pinned by the unique constraint. Spelled once here so the emit and the re-drive can't drift.
SOURCE_RUN = "sessions.Run"
SOURCE_BATCH = "sessions.Batch"
SOURCE_SESSION = "sessions.Session"


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
    """Rank a classified run for a list that is then capped.

    An unranked status sorts *first* — under a cap, being dropped is the one outcome a reader
    cannot recover from.
    """
    from sessions.models import EnvelopeStatus

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


def notification_source_for_watch(session, pipeline_id: int) -> tuple[str, str, EventType]:
    """The key a pipeline-watch give-up is deduped on.

    Scoped to the pipeline, not the thread: a ``thread_id`` outlives the merge request and a watch
    can be re-armed with a fresh budget, so a thread-only key would mute every later give-up.
    """
    return SOURCE_SESSION, f"{session.thread_id}:{pipeline_id}", EventType.PIPELINE_WATCH_EXHAUSTED


def batch_status_tone(notable: int, total: int) -> str:
    """Single source of truth for a batch's aggregate tone, shared by the email pill and the
    RocketChat attachment so the two channels can never disagree. ``notable == 0`` is all-clear
    (only reachable defensively — a notified batch always has notable > 0)."""
    if notable == 0:
        return "success"
    if notable == total:
        return "failure"
    return "warning"


def status_tones() -> dict[str, str]:
    """Every status's channel tone. ``all-clear`` is spelled out rather than left to fall through,
    which is what makes an untoned status detectable."""
    from sessions.models import EnvelopeStatus

    return {
        EnvelopeStatus.FAILED: "failure",
        EnvelopeStatus.FOUND_ISSUES: "warning",
        EnvelopeStatus.NEEDS_ATTENTION: "warning",
        EnvelopeStatus.ALL_CLEAR: "success",
    }


def envelope_tone(status: str) -> str:
    """A single run's tone, shared by the email pill and the RocketChat attachment so the two
    channels agree. An untoned status renders as a failure, never green."""
    if (tone := status_tones().get(status)) is None:
        logger.error("envelope_tone: untoned envelope status %r, rendering it as a failure", status)
        return "failure"
    return tone
