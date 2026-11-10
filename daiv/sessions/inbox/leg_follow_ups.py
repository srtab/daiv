"""A coordinator's leg follow-up on the leg's side: a ``leg_follow_up`` inbox event posted with a QUEUED delegated run
of its own (``specs/002-delegated-leg-follow-ups``).

The run carries the delegate call's ``batch_id``, so the batch counts the leg follow-up from the moment it is posted,
and status, cancel and the coordinator's resume cover it as they cover a new leg. It is not a next run: its trigger type
is outside ``INBOX_TRIGGERS``, and ``sessions.queue`` leaves QUEUED runs with inbox events alone, so only the drain
starts it, alone, with the branch's remembered events (:mod:`sessions.inbox.splits`). Posting does not drain: the
delegator drains the leg's session once every run of its batch exists, and the per-minute sweep is the backstop.

The event ends with its run and is never retried (:meth:`LegFollowUps.end_with_run`). It stops at its start, without
running, when the leg's merge request was merged or closed after it was posted (:meth:`LegFollowUps.stop`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from asgiref.sync import sync_to_async

from sessions.inbox.kinds import SourceKey
from sessions.legs import NOT_RUN_CLOSED, NOT_RUN_MERGED, Leg
from sessions.live.notify import session_changed
from sessions.models import (
    EventFailure,
    EventKind,
    EventState,
    InboxEvent,
    MergeRequestState,
    Run,
    RunStatus,
    Session,
    SessionOrigin,
)

if TYPE_CHECKING:
    import uuid

    from accounts.models import User

BUSY = "busy"

_STOPS = {
    MergeRequestState.MERGED: (EventFailure.MR_MERGED, NOT_RUN_MERGED),
    MergeRequestState.CLOSED: (EventFailure.MR_CLOSED, NOT_RUN_CLOSED),
}
_NOT_RUN = dict(_STOPS.values())


class LegFollowUpRefused(Exception):  # noqa: N818
    """The leg follow-up was not posted: ``reason`` is :data:`BUSY`, or the ``MergeRequestState`` of the leg's merge
    request, merged or closed."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class LegFollowUps:
    """The leg follow-ups of ``leg``."""

    def __init__(self, leg: Leg):
        self.leg = leg

    def post(self, *, prompt: str, batch_id: uuid.UUID, user: User, muted: bool | None, references: list[dict]) -> Run:
        """Create the ``leg_follow_up`` event and its QUEUED delegated run in one transaction, with the leg's session
        row locked, and return the run. Raises :class:`LegFollowUpRefused` when, under that lock, delegated work on the
        leg is unfinished or its merge request was merged or closed. SYNC ONLY."""
        with transaction.atomic():
            session = Session.objects.select_for_update().get(pk=self.leg.session.pk)
            leg = Leg(session)
            if leg.is_busy():
                raise LegFollowUpRefused(BUSY)
            if (state := leg.merge_request_state()) != MergeRequestState.OPEN:
                raise LegFollowUpRefused(state)
            payload: dict = {"prompt": prompt, "batch_id": str(batch_id)}
            if references:
                payload["references"] = list(references)
            if muted is not None:
                payload["muted"] = muted
            run = Run.objects.create(
                session=session,
                trigger_type=SessionOrigin.DELEGATED_JOB,
                status=RunStatus.QUEUED,
                batch_id=batch_id,
                user=user,
                muted=muted,
                prompt=prompt,
                repo_id=session.repo_id,
                ref=session.ref,
                sandbox_environment_id=session.sandbox_environment_id,
            )
            InboxEvent.objects.create(
                session=session,
                kind=EventKind.LEG_FOLLOW_UP,
                source_key=SourceKey.leg_follow_up(batch_id),
                payload=payload,
                wake=True,
                ref=session.ref,
                author=user,
                run=run,
            )
            session_changed(session.thread_id)
        return run

    async def apost(
        self, *, prompt: str, batch_id: uuid.UUID, user: User, muted: bool | None, references: list[dict]
    ) -> Run:
        return await sync_to_async(self.post)(
            prompt=prompt, batch_id=batch_id, user=user, muted=muted, references=references
        )

    @staticmethod
    def stop_reason(session: Session) -> EventFailure | None:
        """Why a leg follow-up on ``session`` must not start: its leg's merge request was merged or closed."""
        stop = _STOPS.get(Leg(session).merge_request_state())
        return None if stop is None else stop[0]

    @staticmethod
    def stop(event: InboxEvent, failure: EventFailure) -> None:
        """Fail the leg follow-up ``event`` with ``failure`` and finish its run FAILED, never started, with the
        matching not-run label. The finish says ``skip_dispatch``: the caller drains once this commits."""
        now = timezone.now()
        InboxEvent.objects.filter(pk=event.pk).update(state=EventState.FAILED, failure=failure, updated_at=now)
        session_changed(event.session_id)
        Run.objects.finish(
            Run.objects.get(pk=event.run_id),
            RunStatus.FAILED,
            skip_dispatch=True,
            never_started=True,
            error_message=_NOT_RUN[failure],
            finished_at=now,
            started_at=now,
        )

    @staticmethod
    def settle_unstartable(event: InboxEvent, run: Run | None) -> None:
        """Settle the pending leg follow-up ``event`` whose own ``run`` is gone or no longer QUEUED, so the drain moves
        on: as :meth:`end_with_run` settles it, else failed (``run_failed``). SYNC ONLY."""
        if run is not None:
            LegFollowUps.end_with_run(run)
        InboxEvent.objects.filter(pk=event.pk, state=EventState.PENDING).update(
            state=EventState.FAILED, failure=EventFailure.RUN_FAILED, updated_at=timezone.now()
        )
        session_changed(event.session_id)

    @staticmethod
    def end_with_run(run: Run) -> None:
        """Settle the leg follow-up linked to ``run`` once it ended, whether the drain took it or not: dismissed when
        the run was cancelled, failed (``run_failed``) when it failed. A run that succeeded leaves its taken event to
        the shared path, which marks it handled. SYNC ONLY."""
        if run.status != RunStatus.FAILED:
            return
        follow_ups = InboxEvent.objects.filter(
            run=run, kind=EventKind.LEG_FOLLOW_UP, state__in=(EventState.PENDING, EventState.TAKEN)
        )
        if run.is_cancelled:
            moved = follow_ups.update(state=EventState.DISMISSED, updated_at=timezone.now())
        else:
            moved = follow_ups.update(
                state=EventState.FAILED, failure=EventFailure.RUN_FAILED, updated_at=timezone.now()
            )
        if moved:
            session_changed(run.session_id)
