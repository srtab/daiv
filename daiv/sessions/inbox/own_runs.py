"""Inbox events posted with a QUEUED run of their own (``OWN_RUN_KINDS``), which only they run: a leg follow-up
(:mod:`sessions.inbox.leg_follow_ups`) and a merge request job (:mod:`sessions.inbox.merge_request_jobs`).

Such an event ends with its run and is never retried (:meth:`OwnRunEvents.end_with_run`). The drain stops it at its
start, without running, when its session was archived or its merge request was merged or closed
(:meth:`OwnRunEvents.stop`), and settles it when its run is gone or no longer QUEUED
(:meth:`OwnRunEvents.settle_unstartable`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from django.utils import timezone

from sessions.live.notify import session_changed
from sessions.models import EventFailure, EventState, InboxEvent, MergeRequestState, Run, RunStatus

if TYPE_CHECKING:
    from sessions.models import Session

MERGE_REQUEST_STOPS = {
    MergeRequestState.MERGED: EventFailure.MR_MERGED,
    MergeRequestState.CLOSED: EventFailure.MR_CLOSED,
}


class OwnRunEvents:
    """The events of ``kind``, each posted with a run of its own. ``not_run`` is the error message a run stopped for
    each failure ends with."""

    kind: ClassVar[str]
    not_run: ClassVar[dict[EventFailure, str]]

    @classmethod
    def merge_request_state(cls, session: Session, event: InboxEvent) -> MergeRequestState:
        raise NotImplementedError

    @classmethod
    def stop_reason(cls, session: Session, event: InboxEvent) -> EventFailure | None:
        """Why ``event`` on ``session`` must not start: its session was archived, or its merge request was merged or
        closed. SYNC ONLY."""
        if session.archived_at is not None:
            return EventFailure.SESSION_ARCHIVED
        return MERGE_REQUEST_STOPS.get(cls.merge_request_state(session, event))

    @classmethod
    def stop(cls, event: InboxEvent, failure: EventFailure, run: Run) -> None:
        """Fail ``event`` with ``failure`` and finish its ``run`` FAILED, never started, with why. The finish says
        ``skip_dispatch``: the caller drains once this commits. SYNC ONLY."""
        now = timezone.now()
        InboxEvent.objects.filter(pk=event.pk).update(state=EventState.FAILED, failure=failure, updated_at=now)
        session_changed(event.session_id)
        Run.objects.finish(
            run,
            RunStatus.FAILED,
            skip_dispatch=True,
            never_started=True,
            error_message=cls.not_run[failure],
            finished_at=now,
            started_at=now,
        )
        cls._stopped(event, failure, run)

    @classmethod
    def settle_unstartable(cls, event: InboxEvent, run: Run | None) -> None:
        """Settle the pending ``event`` whose own ``run`` is gone or no longer QUEUED, so the drain moves on: as
        :meth:`end_with_run` settles it when that run failed, else failed (``run_failed``). SYNC ONLY."""
        if run is not None and run.status == RunStatus.FAILED:
            cls.end_with_run(run)
        elif InboxEvent.objects.filter(pk=event.pk, state=EventState.PENDING).update(
            state=EventState.FAILED, failure=EventFailure.RUN_FAILED, updated_at=timezone.now()
        ):
            session_changed(event.session_id)

    @classmethod
    def end_with_run(cls, run: Run) -> None:
        """Settle the event linked to ``run`` once it ended FAILED, whether the drain took it or not: dismissed when the
        run was cancelled, else failed (``run_failed``). A run that succeeded leaves its taken event to the shared path,
        which marks it handled. SYNC ONLY."""
        if run.status != RunStatus.FAILED:
            return
        unsettled = InboxEvent.objects.filter(run=run, kind=cls.kind, state__in=(EventState.PENDING, EventState.TAKEN))
        events = list(unsettled)
        now = timezone.now()
        if run.is_cancelled:
            moved = unsettled.update(state=EventState.DISMISSED, updated_at=now)
        else:
            moved = unsettled.update(state=EventState.FAILED, failure=EventFailure.RUN_FAILED, updated_at=now)
            if moved:
                cls._failed(run, events)
        if moved:
            session_changed(run.session_id)

    @classmethod
    def _stopped(cls, event: InboxEvent, failure: EventFailure, run: Run) -> None:
        pass

    @classmethod
    def _failed(cls, run: Run, events: list[InboxEvent]) -> None:
        pass
