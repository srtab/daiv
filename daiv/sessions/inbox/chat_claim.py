"""The events an inline chat turn takes from its inbox (``specs/001-session-inbox/contracts/inbox-service.md``).

The turn takes them under its holder id right after it claims the slot, before its ``Run`` exists; the ``Run`` takes
them over once its row exists, and a turn that ends before that puts them back.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Exists, OuterRef, Q
from django.utils import timezone

from asgiref.sync import sync_to_async

from sessions.inbox.next_runs import NextRuns
from sessions.inbox.pending import PendingEvents
from sessions.models import EventState, InboxEvent, Run, RunStatus, Session


class ChatClaim:
    """The events the inline chat turn ``holder_id`` takes on the session ``thread_id``."""

    def __init__(self, thread_id: str, holder_id: str):
        self.thread_id = thread_id
        self.holder_id = holder_id

    def take(self) -> list[InboxEvent]:
        """Take the session's pending remembered events for its branch for the chat turn that just claimed the slot
        under ``holder_id``, before its ``Run`` exists. SYNC ONLY.

        A waking event is left for the drain, which starts its run once the turn ends: only the drain renders a waking
        event's request and charges a CI attempt. It is still superseded by a newer event of its subject, so a turn that
        takes a passing pipeline leaves no older failure behind to wake a fix run, nor a next run with nothing to do.
        """
        with transaction.atomic():
            session = Session.objects.select_for_update().get(pk=self.thread_id)
            pending = list(
                InboxEvent.objects.filter(
                    Q(ref="") | Q(ref=session.ref), session=session, state=EventState.PENDING
                ).order_by("created_at", "id")
            )
            survivors = PendingEvents(session, pending).supersede()
            superseded_runs = {event.run_id for event in pending if event.run_id is not None and event not in survivors}
            NextRuns(session).drop(
                Run.objects.filter(pk__in=superseded_runs, status=RunStatus.QUEUED).exclude(
                    Exists(InboxEvent.objects.filter(run=OuterRef("pk"), state=EventState.PENDING, wake=True))
                )
            )
            taken = [event for event in survivors if not event.wake]
            InboxEvent.objects.filter(pk__in=[event.pk for event in taken]).update(
                state=EventState.TAKEN, claim_token=self.holder_id, updated_at=timezone.now()
            )
        return taken

    async def atake(self) -> list[InboxEvent]:
        return await sync_to_async(self.take)()

    async def alink(self, run: Run) -> None:
        """Point the events the chat turn took under ``holder_id`` at its ``Run``, now that the row exists."""
        await InboxEvent.objects.filter(
            session_id=run.session_id, claim_token=self.holder_id, state=EventState.TAKEN
        ).aupdate(run=run, claim_token="")
        await (
            InboxEvent.objects
            .filter(session_id=run.session_id, state=EventState.SUPERSEDED, run__isnull=True, claim_token="")
            .filter(subject__in=InboxEvent.objects.filter(run=run).exclude(subject="").values("subject"))
            .aupdate(run=run)
        )

    async def arelease(self) -> int:
        """Put back for the session's next turn the events the chat turn ``holder_id`` took and no ``Run`` took over;
        returns how many. Such a turn ended before its ``Run`` existed, so the events never reached the model and cost
        no attempt."""
        return await InboxEvent.objects.filter(
            session_id=self.thread_id, claim_token=self.holder_id, state=EventState.TAKEN, run__isnull=True
        ).aupdate(state=EventState.PENDING, claim_token="", updated_at=timezone.now())
