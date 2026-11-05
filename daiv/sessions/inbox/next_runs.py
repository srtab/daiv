"""A session's next runs: the QUEUED runs its pending events wait in (``specs/001-session-inbox/``)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef
from django.utils import timezone

from asgiref.sync import sync_to_async

from sessions.executor.lock import SessionLock
from sessions.inbox.kinds import INBOX_TRIGGERS, PRECEDENCE, EventContent
from sessions.inbox.render import RunInput
from sessions.inbox.requester import Requester
from sessions.inbox.splits import RequestSplits
from sessions.models import EventState, InboxEvent, Run, RunStatus, SessionOrigin

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.db.models import QuerySet

    from sessions.models import Session

logger = logging.getLogger("daiv.sessions")


class NextRuns:
    """``session``'s next runs, one per branch and requester."""

    def __init__(self, session: Session):
        self.session = session

    def queued(self) -> QuerySet[Run]:
        """The session's QUEUED next runs, oldest first."""
        return Run.objects.filter(
            Exists(InboxEvent.objects.filter(run=OuterRef("pk"))),
            session=self.session,
            status=RunStatus.QUEUED,
            task_result__isnull=True,
            trigger_type__in=INBOX_TRIGGERS,
        ).order_by("created_at")

    def for_branch(self, ref: str, requester: Requester) -> Run | None:
        """The QUEUED next run for the branch ``ref`` and ``requester``. SYNC ONLY."""
        return self.queued().filter(ref=ref, **requester.run_lookup()).first()

    def is_busy(self) -> bool:
        """Whether a message sent to the session now has to wait: a live holder has the slot, or a next run is queued
        ahead of it. SYNC ONLY."""
        return SessionLock.is_held(self.session) or self.queued().exists()

    async def ais_busy(self) -> bool:
        return await sync_to_async(self.is_busy)()

    def job_run_active(self) -> bool:
        """Whether a job run holds the session's one active job place, which no run can start from the inbox before it
        ends, even one queued for a worker that has not claimed the slot yet. SYNC ONLY."""
        return Run.objects.filter(
            session=self.session,
            status__in=(RunStatus.READY, RunStatus.RUNNING),
            trigger_type__in=SessionOrigin.one_active(),
        ).exists()

    def join(self, event: InboxEvent) -> Run | None:
        """Link the waking ``event`` to the next run it joins, and return that run: the existing one for its branch and
        requester, or a new one while the session is busy or another next run waits. ``None`` when the session is idle,
        or a QUEUED run the inbox did not create holds that place (``run_one_next_run_per_user``,
        ``run_one_next_run_per_account``); the event then waits for the drain after it."""
        requester = Requester.of_event(event, self.session)
        run = self.for_branch(event.ref, requester)
        if run is None and (self.is_busy() or self.job_run_active()):
            run = self._create(event, requester)
        if run is not None:
            InboxEvent.objects.filter(pk=event.pk).update(run=run)
            event.run = run
            self.describe(run)
        return run

    def move(self, run: Run, ref: str) -> bool:
        """Move the next run ``run`` to the branch ``ref``, unless a QUEUED run already holds its place there."""
        try:
            with transaction.atomic():
                Run.objects.filter(pk=run.pk).update(ref=ref)
        except IntegrityError:
            return False
        run.ref = ref
        return True

    def drop(self, runs: Iterable[Run | None]) -> None:
        """Delete ``runs``, next runs left with no waking event to start them, detaching their events."""
        if ids := [run.pk for run in runs if run is not None]:
            InboxEvent.objects.filter(run_id__in=ids).update(run=None, updated_at=timezone.now())
            Run.objects.filter(pk__in=ids).delete()

    def drop_emptied(self, runs: Iterable[Run]) -> None:
        """Delete the next runs among ``runs`` the take left with no event, whose requests went to another run (one sent
        without a branch matches any); ``sessions.queue`` would otherwise start them as plain jobs."""
        self.drop(
            Run.objects.filter(pk__in=[run.pk for run in runs], status=RunStatus.QUEUED).exclude(
                Exists(InboxEvent.objects.filter(run=OuterRef("pk")))
            )
        )

    def _create(self, event: InboxEvent, requester: Requester) -> Run | None:
        try:
            with transaction.atomic():
                return Run.objects.create(
                    session=self.session,
                    trigger_type=EventContent.of(event).run_trigger_type(),
                    status=RunStatus.QUEUED,
                    repo_id=self.session.repo_id,
                    ref=event.ref,
                    merge_request_iid=event.merge_request_iid,
                    sandbox_environment_id=self.session.sandbox_environment_id,
                    **requester.run_fields(event.author_username),
                )
        except IntegrityError:
            logger.info(
                "inbox: an older queued run holds thread_id=%s's place on %r; event=%s waits for it",
                self.session.thread_id,
                event.ref,
                event.pk,
            )
            return None

    def describe(self, run: Run) -> None:
        """Keep the next run's prompt and provisional trigger type in step with the requests it will take, the last of
        those waiting for it (:class:`sessions.inbox.splits.RequestSplits`), so the session page shows what it will do
        while it waits."""
        waiting = InboxEvent.objects.filter(run=run, state=EventState.PENDING, wake=True).order_by("created_at", "id")
        requests = RequestSplits(list(waiting.select_related("author"))).splits[-1]
        top = min(requests, key=lambda event: (PRECEDENCE[event.kind], event.created_at))
        Run.objects.filter(pk=run.pk).update(
            prompt=RunInput(requests).request(), trigger_type=EventContent.of(top).run_trigger_type()
        )
