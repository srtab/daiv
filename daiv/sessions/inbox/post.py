"""Posting an event to a session's inbox, the only way in (``specs/001-session-inbox/contracts/inbox-service.md``)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from django.db import transaction

from asgiref.sync import sync_to_async

from sessions.inbox.drain import drain_logged
from sessions.inbox.kinds import KEY_MAX_LENGTH, WAKING_KINDS, EventContent, SourceKey, Subject
from sessions.inbox.next_runs import NextRuns
from sessions.models import EventKind, EventState, InboxEvent, Run, Session

if TYPE_CHECKING:
    import uuid
    from collections.abc import Mapping

    from accounts.models import User
    from sessions.inbox.kinds import DelegatedOutcome
    from sessions.models import QuietReason

logger = logging.getLogger("daiv.sessions")


@dataclass(frozen=True)
class PostResult:
    """``run_id`` is the run that will handle or took the event: the session's next run when it is busy, the run the
    drain started when it was idle and nothing held the caller's transaction open, else ``None``."""

    event: InboxEvent
    created: bool
    run_id: uuid.UUID | None


def post_event(
    thread_id: str,
    *,
    kind: str,
    source_key: str,
    payload: Mapping[str, Any],
    wake: bool,
    subject: str = "",
    ref: str = "",
    merge_request_iid: int | None = None,
    reason: QuietReason | None = None,
    author: User | None = None,
    author_username: str = "",
    author_provider: str = "",
    author_uid: str = "",
) -> PostResult:
    """Record an event for the session ``thread_id``, once per ``source_key``, and drain the session once it commits
    when the event wakes the agent. SYNC ONLY; :func:`apost_event` is the async interface.

    A waking event on a busy session joins the session's next run for the event's branch and requester
    (:meth:`sessions.inbox.requester.Requester.of_event`), which is created when there is none yet. A waking event
    with no ``ref`` concerns the session's own branch. ``author_provider``/``author_uid``: the platform account of an
    author no DAIV user is linked to.

    Raises ``ValueError`` for an invalid payload, or ``wake`` on a kind that never wakes, and
    ``Session.DoesNotExist`` when there is no such session.
    """
    EventContent(kind, payload).validate_payload()
    if wake and kind not in WAKING_KINDS:
        raise ValueError(f"a {kind} event never wakes the agent")
    fields = {
        "kind": kind,
        "source_key": source_key[:KEY_MAX_LENGTH],
        "payload": dict(payload),
        "subject": subject,
        "merge_request_iid": merge_request_iid,
        "reason": reason or "",
        "author": author,
        "author_username": author_username,
        "author_provider": author_provider,
        "author_uid": author_uid,
    }
    with transaction.atomic():
        session = Session.objects.select_for_update().get(pk=thread_id)
        event = InboxEvent(session=session, wake=wake, ref=ref or (session.ref if wake else ""), **fields)
        InboxEvent.objects.bulk_create([event], ignore_conflicts=True)
        stored = InboxEvent.objects.get(session=session, source_key=event.source_key)
        if stored.pk != event.pk:
            return PostResult(event=stored, created=False, run_id=stored.run_id)
        if wake:
            NextRuns(session).join(stored)
            transaction.on_commit(partial(drain_logged, thread_id), robust=True)
    if wake and stored.run_id is None:
        stored.run_id = InboxEvent.objects.filter(pk=stored.pk).values_list("run_id", flat=True).first()
    return PostResult(event=stored, created=True, run_id=stored.run_id)


apost_event = sync_to_async(post_event)


def post_delegated_ci_outcome(
    child: Session, *, merge_request_iid: int, pipeline_id: int, outcome: DelegatedOutcome, web_url: str
) -> None:
    """Remind ``child``'s parent session, when it has one, how CI ended on a merge request ``child`` owns. Logs a
    failure instead of raising, since the child's own event already landed. SYNC ONLY."""
    if child.parent_id is None:
        return
    try:
        post_event(
            child.parent_id,
            kind=EventKind.DELEGATED_CI_OUTCOME,
            source_key=SourceKey.child_ci(child.thread_id, pipeline_id, outcome),
            payload={
                "child_thread_id": child.thread_id,
                "merge_request_iid": merge_request_iid,
                "outcome": outcome,
                "web_url": web_url,
            },
            wake=False,
            subject=Subject.child_ci(child.thread_id, merge_request_iid),
        )
    except Exception:
        logger.exception(
            "inbox: could not tell the parent of thread_id=%s how CI ended on !%s", child.thread_id, merge_request_iid
        )


apost_delegated_ci_outcome = sync_to_async(post_delegated_ci_outcome)


def attach_next_run(event: InboxEvent) -> Run | None:
    """The run that handles the waking ``event``: the one it joined or that took it, else, while its session is busy,
    its branch's next run, created when there is none. ``None`` when the event cannot run (it failed or was
    dismissed), the session is idle, or an older queued run holds the branch's place. SYNC ONLY.

    For a caller that must answer with a run when the drain on post did not start one: the slot may have been claimed
    after the post committed, which leaves the event waiting with no run to name."""
    with transaction.atomic():
        session = Session.objects.select_for_update().get(pk=event.session_id)
        event = InboxEvent.objects.select_related("author", "run").get(pk=event.pk)
        if event.run_id is not None or event.state != EventState.PENDING:
            return event.run
        if (run := NextRuns(session).join(event)) is not None:
            transaction.on_commit(partial(drain_logged, session.thread_id), robust=True)
        return run


aattach_next_run = sync_to_async(attach_next_run)
