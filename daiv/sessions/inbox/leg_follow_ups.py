"""A coordinator's leg follow-up on the leg's side: a ``leg_follow_up`` inbox event posted with a QUEUED delegated run
of its own (``specs/002-delegated-leg-follow-ups``).

The run carries the delegate call's ``batch_id``, so the batch counts the leg follow-up from the moment it is posted,
and status, cancel and the coordinator's resume cover it as they cover a new leg. It is not a next run: its trigger type
is outside ``INBOX_TRIGGERS``, and ``sessions.queue`` leaves QUEUED runs with inbox events alone, so only the drain
starts it, alone, with the branch's remembered events (:mod:`sessions.inbox.splits`). Posting does not drain: the
delegator drains the leg's session once every run of its batch exists, and the per-minute sweep is the backstop.

The event ends with its run and is never retried. It stops at its start, without running, when the leg's merge request
was merged or closed after it was posted, and as soon as the drain sees the leg's session archived, so its batch still
finishes (:mod:`sessions.inbox.own_runs`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, override

from django.db import transaction

from asgiref.sync import sync_to_async

from sessions.inbox.kinds import SourceKey
from sessions.inbox.own_runs import OwnRunEvents
from sessions.legs import NOT_RUN_ARCHIVED, NOT_RUN_CLOSED, NOT_RUN_MERGED, Leg
from sessions.live.notify import session_changed
from sessions.models import (
    EventFailure,
    EventKind,
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
ARCHIVED = "archived"


class LegFollowUpRefused(Exception):  # noqa: N818
    """The leg follow-up was not posted: ``reason`` is :data:`BUSY`, :data:`ARCHIVED`, or the ``MergeRequestState`` of
    the leg's merge request, merged or closed."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class LegFollowUps(OwnRunEvents):
    """The leg follow-ups of ``leg``."""

    kind = EventKind.LEG_FOLLOW_UP
    not_run: ClassVar[dict[EventFailure, str]] = {
        EventFailure.MR_MERGED: NOT_RUN_MERGED,
        EventFailure.MR_CLOSED: NOT_RUN_CLOSED,
        EventFailure.SESSION_ARCHIVED: NOT_RUN_ARCHIVED,
    }

    def __init__(self, leg: Leg):
        self.leg = leg

    def post(self, *, prompt: str, batch_id: uuid.UUID, user: User, muted: bool | None, references: list[dict]) -> Run:
        """Create the ``leg_follow_up`` event and its QUEUED delegated run in one transaction, with the leg's session
        row locked, and return the run. Raises :class:`LegFollowUpRefused` when, under that lock, the leg's session is
        archived, delegated work on it is unfinished or its merge request was merged or closed. SYNC ONLY."""
        with transaction.atomic():
            session = Session.objects.select_for_update().get(pk=self.leg.session.pk)
            leg = Leg(session)
            if leg.is_archived():
                raise LegFollowUpRefused(ARCHIVED)
            if leg.is_busy():
                raise LegFollowUpRefused(BUSY)
            if not (state := leg.merge_request_state()).is_open:
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

    @classmethod
    @override
    def merge_request_state(cls, session: Session, event: InboxEvent) -> MergeRequestState:
        return Leg(session).merge_request_state()
