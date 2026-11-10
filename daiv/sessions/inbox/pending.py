"""A session's pending inbox events, as the drain and Up next read them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.utils import timezone

from sessions.inbox.next_runs import NextRuns
from sessions.inbox.requester import Requester
from sessions.live.notify import session_changed
from sessions.models import EventKind, EventState, InboxEvent

if TYPE_CHECKING:
    import uuid

    from sessions.models import Run, Session


class PendingEvents:
    """``events``, a session's pending events oldest first."""

    def __init__(self, session: Session, events: list[InboxEvent]):
        self.session = session
        self.events = events

    def branch_of(self, event: InboxEvent) -> str:
        """The branch whose run a pending ``event`` waits for now
        (:meth:`sessions.inbox.next_runs.NextRuns.branch_of`)."""
        return NextRuns(self.session).branch_of(event)

    def newest_of_each_subject(self) -> list[InboxEvent]:
        """The events less those a newer one of the same subject, about the same repository, replaces, oldest first."""
        newest = {event.subject_key: event for event in self.events if event.subject}
        return [event for event in self.events if not event.subject or newest[event.subject_key] is event]

    def supersede(self) -> list[InboxEvent]:
        """Mark all but the newest event per subject ``superseded``, and return the rest, oldest first."""
        survivors = self.newest_of_each_subject()
        if replaced := [event for event in self.events if event not in survivors]:
            InboxEvent.objects.filter(pk__in=[event.pk for event in replaced]).update(
                state=EventState.SUPERSEDED, run=None, updated_at=timezone.now()
            )
            session_changed(self.session.thread_id)
        return survivors

    def follow_session_branch(self, queued_runs: list[Run]) -> None:
        """Point the requests that follow the session (:meth:`sessions.inbox.kinds.EventContent.follows_session`) at
        its branch, which the run they waited for may have moved by publishing to a new one. A next run that only such
        requests wait in moves with them, keeping the id a follow-up job's caller polls; the others let them go. A leg
        follow-up keeps its own run, which the take moves to the branch. An event about another of the session's
        repositories keeps its ``ref``, that repository's branch, and only leaves or moves the next run it waits in."""
        session = self.session
        queued_refs = {run.pk: run.ref for run in queued_runs}
        moved = [event for event in self.events if self._waiting_on(event, queued_refs) != self.branch_of(event)]
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
        InboxEvent.objects.filter(pk__in=[event.pk for event in moved if not event.repo_id]).update(
            ref=session.ref, updated_at=timezone.now()
        )
        released = [event for event in moved if event.run_id not in kept and event.kind != EventKind.LEG_FOLLOW_UP]
        InboxEvent.objects.filter(pk__in=[event.pk for event in released]).update(run=None, updated_at=timezone.now())
        session_changed(session.thread_id)
        for event in moved:
            if not event.repo_id:
                event.ref = session.ref
        for event in released:
            event.run_id = None

    def _waiting_on(self, event: InboxEvent, queued_refs: dict[uuid.UUID, str]) -> str:
        """The branch ``event`` waits on: its ``ref``, or, for an event about another repository, whose ``ref`` is that
        repository's branch, the branch of the next run it joined."""
        if event.repo_id:
            return queued_refs.get(event.run_id, self.session.ref)
        return event.ref
