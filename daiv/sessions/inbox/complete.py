"""Settling the events a run took once it ends, then starting the session's next run (research.md R12).

| Run outcome | Its taken events |
|---|---|
| SUCCESSFUL or WAITING_INPUT | ``handled`` |
| FAILED after its owner asked it to stop | ``dismissed``: the owner chose to stop, so they are not retried |
| FAILED otherwise | back to ``pending`` with ``attempts + 1``, or ``failed`` (``retries_exhausted``) at
  ``MAX_EVENT_ATTEMPTS``, logged with ``logger.error`` |

The sweep (``sessions.tasks.drain_inboxes_cron_task``) settles events the same way when their run's slot went stale
before the run ended.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db.models import Q
from django.utils import timezone

from asgiref.sync import sync_to_async

from sessions.executor.lock import stale_cutoff
from sessions.inbox.drain import drain_logged
from sessions.models import EventFailure, EventState, InboxEvent, Run, RunStatus

if TYPE_CHECKING:
    from django.db.models import QuerySet
logger = logging.getLogger("daiv.sessions")

MAX_EVENT_ATTEMPTS = 3


def on_run_ended(run: Run) -> None:
    """Settle the events ``run`` took, then drain its session. Never raises. SYNC ONLY."""
    try:
        settle_events(run)
    except Exception:
        logger.exception("inbox: settling the events of run=%s failed; the sweep retries them", run.pk)
    if run.session_id:
        drain_logged(run.session_id)


aon_run_ended = sync_to_async(on_run_ended)


def settle_events(run: Run) -> None:
    """Move the events ``run`` took to the state its outcome calls for. SYNC ONLY."""
    taken = InboxEvent.objects.filter(run=run, state=EventState.TAKEN)
    if run.status in (RunStatus.SUCCESSFUL, RunStatus.WAITING_INPUT):
        taken.update(state=EventState.HANDLED, updated_at=timezone.now())
    elif run.status == RunStatus.FAILED and run.cancel_requested_at is not None:
        taken.update(state=EventState.DISMISSED, updated_at=timezone.now())
    elif run.status == RunStatus.FAILED:
        retry(taken, cause=f"run={run.pk} failed")


def retry(events: QuerySet[InboxEvent], *, cause: str) -> None:
    """Put ``events`` back for the next run, or fail the ones out of attempts. SYNC ONLY."""
    exhausted: list[InboxEvent] = []
    for event in events:
        event.attempts += 1
        if event.attempts >= MAX_EVENT_ATTEMPTS:
            event.state, event.failure = EventState.FAILED, EventFailure.RETRIES_EXHAUSTED
            exhausted.append(event)
        else:
            event.state, event.run, event.claim_token = EventState.PENDING, None, ""
        event.save(update_fields=["attempts", "state", "failure", "run", "claim_token", "updated_at"])
    if exhausted:
        logger.error(
            "inbox: %d event(s) of thread_id=%s failed after %d attempts (%s): %s",
            len(exhausted),
            exhausted[0].session_id,
            MAX_EVENT_ATTEMPTS,
            cause,
            ", ".join(f"{event.kind}:{event.source_key}" for event in exhausted),
        )


def recover_stale_events() -> int:
    """Settle the events taken on sessions whose slot went stale: by their run's outcome when it ended, else as a
    failed attempt, since their run will never end. Returns how many events moved. SYNC ONLY."""
    stale = InboxEvent.objects.filter(state=EventState.TAKEN, session__last_active_at__lt=stale_cutoff())
    moved = 0
    for run in Run.objects.filter(pk__in=stale.filter(run__status__in=RunStatus.terminal()).values("run_id")):
        moved += InboxEvent.objects.filter(run=run, state=EventState.TAKEN).count()
        settle_events(run)
    orphaned = stale.filter(Q(run__isnull=True) | ~Q(run__status__in=RunStatus.terminal()))
    for thread_id in set(orphaned.values_list("session_id", flat=True)):
        events = orphaned.filter(session_id=thread_id)
        moved += events.count()
        retry(events, cause="its run's slot went stale before the run ended")
    return moved
