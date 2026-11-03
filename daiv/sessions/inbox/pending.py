"""A session's pending inbox events, as the drain and Up next read them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils import timezone

from sessions.inbox.kinds import EventContent
from sessions.inbox.next_runs import NextRuns
from sessions.inbox.requester import Requester
from sessions.models import EventState, InboxEvent

if TYPE_CHECKING:
    from sessions.models import Run, Session


class PendingEvents:
    """``events``, a session's pending events oldest first."""

    def __init__(self, session: Session, events: list[InboxEvent]):
        self.session = session
        self.events = events

    def branch_of(self, event: InboxEvent) -> str:
        """The branch a pending ``event`` concerns now: the session's for a request that follows it
        (:meth:`sessions.inbox.kinds.EventContent.follows_session`), else the one it was posted on."""
        return self.session.ref if EventContent.of(event).follows_session() else event.ref

    def newest_of_each_subject(self) -> list[InboxEvent]:
        """The events less those a newer one of the same subject replaces, oldest first."""
        newest = {event.subject: event for event in self.events if event.subject}
        return [event for event in self.events if not event.subject or newest[event.subject] is event]

    def supersede(self) -> list[InboxEvent]:
        """Mark all but the newest event per subject ``superseded``, and return the rest, oldest first."""
        survivors = self.newest_of_each_subject()
        if replaced := [event for event in self.events if event not in survivors]:
            InboxEvent.objects.filter(pk__in=[event.pk for event in replaced]).update(
                state=EventState.SUPERSEDED, run=None, updated_at=timezone.now()
            )
        return survivors

    def follow_session_branch(self, queued_runs: list[Run]) -> None:
        """Point the requests that follow the session (:meth:`sessions.inbox.kinds.EventContent.follows_session`) at
        its branch, which the run they waited for may have moved by publishing to a new one. A next run that only such
        requests wait in moves with them, keeping the id a follow-up job's caller polls; the others let them go."""
        session = self.session
        moved = [event for event in self.events if self.branch_of(event) != event.ref]
        if not moved:
            return
        next_runs = NextRuns(session)
        held = {(run.ref, Requester.of_run(run)) for run in queued_runs}
        kept = set()
        for run in queued_runs:
            riders = [event for event in self.events if event.run_id == run.pk]
            key = (session.ref, Requester.of_run(run))
            if (
                riders
                and all(event in moved for event in riders)
                and key not in held
                and next_runs.move(run, session.ref)
            ):
                held.add(key)
                kept.add(run.pk)
        InboxEvent.objects.filter(pk__in=[event.pk for event in moved]).update(
            ref=session.ref, updated_at=timezone.now()
        )
        InboxEvent.objects.filter(pk__in=[event.pk for event in moved if event.run_id not in kept]).update(run=None)
        for event in moved:
            event.ref = session.ref
            if event.run_id not in kept:
                event.run_id = None
