"""Posting an event to a session's inbox, the only way in (``specs/001-session-inbox/contracts/inbox-service.md``)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from django.db import transaction

from asgiref.sync import sync_to_async

from sessions.inbox.drain import drain_logged, job_run_active, next_run_for, slot_busy
from sessions.inbox.kinds import (
    KEY_MAX_LENGTH,
    PRECEDENCE,
    WAKING_KINDS,
    child_ci_key,
    child_ci_subject,
    run_trigger_type,
    validate_payload,
)
from sessions.inbox.render import render_request
from sessions.models import EventKind, EventState, InboxEvent, Run, RunStatus, Session

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
) -> PostResult:
    """Record an event for the session ``thread_id``, once per ``source_key``, and drain the session once it commits
    when the event wakes the agent. SYNC ONLY; :func:`apost_event` is the async interface.

    A waking event on a busy session joins the session's next run for the event's branch, which is created when there
    is none yet. A waking event with no ``ref`` concerns the session's own branch.

    Raises ``ValueError`` for an invalid payload, or ``wake`` on a kind that never wakes, and
    ``Session.DoesNotExist`` when there is no such session.
    """
    validate_payload(kind, payload)
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
    }
    with transaction.atomic():
        session = Session.objects.select_for_update().get(pk=thread_id)
        event = InboxEvent(session=session, wake=wake, ref=ref or (session.ref if wake else ""), **fields)
        InboxEvent.objects.bulk_create([event], ignore_conflicts=True)
        stored = InboxEvent.objects.get(session=session, source_key=event.source_key)
        if stored.pk != event.pk:
            return PostResult(event=stored, created=False, run_id=stored.run_id)
        if wake:
            if (run := _next_run(session, stored)) is not None:
                InboxEvent.objects.filter(pk=stored.pk).update(run=run)
                stored.run = run
                _describe_next_run(run)
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
            source_key=child_ci_key(child.thread_id, pipeline_id),
            payload={
                "child_thread_id": child.thread_id,
                "merge_request_iid": merge_request_iid,
                "outcome": outcome,
                "web_url": web_url,
            },
            wake=False,
            subject=child_ci_subject(child.thread_id, merge_request_iid),
        )
    except Exception:
        logger.exception(
            "inbox: could not tell the parent of thread_id=%s how CI ended on !%s", child.thread_id, merge_request_iid
        )


apost_delegated_ci_outcome = sync_to_async(post_delegated_ci_outcome)


def _describe_next_run(run: Run) -> None:
    """Keep the next run's prompt and provisional trigger type in step with the events waiting for it, so the session
    page shows what it will do while it waits."""
    waiting = list(InboxEvent.objects.filter(run=run, state=EventState.PENDING, wake=True).select_related("author"))
    top = min(waiting, key=lambda event: (PRECEDENCE[event.kind], event.created_at))
    Run.objects.filter(pk=run.pk).update(
        prompt=render_request(waiting), trigger_type=run_trigger_type(top.kind, top.payload)
    )


def _next_run(session: Session, event: InboxEvent) -> Run | None:
    """The next run ``event`` joins: the branch's existing one, or a new one while the session is busy."""
    if (run := next_run_for(session, event.ref)) is not None or not (slot_busy(session) or job_run_active(session)):
        return run
    return Run.objects.create(
        session=session,
        trigger_type=run_trigger_type(event.kind, event.payload),
        status=RunStatus.QUEUED,
        repo_id=session.repo_id,
        ref=event.ref,
        user=event.author or session.user,
        merge_request_iid=event.merge_request_iid,
        sandbox_environment_id=session.sandbox_environment_id,
    )
