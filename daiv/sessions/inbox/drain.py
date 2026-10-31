"""Starting a session's next run from its pending events (``specs/001-session-inbox/contracts/inbox-service.md``).

:func:`drain` locks the session row and, unless a live holder has the slot or a job run is active, takes its pending
events in one transaction: it supersedes all but the newest event per subject, turns the CI and delegation wakes the
policy blocks into remembered events, and drops a QUEUED next run left with nothing to wake it. When a waking event
survives, it picks the branch of the oldest one, checks the owner may still run on the repository, promotes that
branch's next run (or creates a READY one), claims the slot for it and marks every pending event for the branch
``taken``; a CI failure among them spends one of its merge request's attempts on the commit it reports. The run is
enqueued once that commits; a failed enqueue frees the slot and puts the events back, for the sweep to retry.

A next run is a QUEUED run with an inbox trigger type, no task yet, and events linked to it. ``sessions.queue`` leaves
such runs alone: only this module starts them, because only it claims the slot for them.
"""

from __future__ import annotations

import logging
from functools import partial
from typing import TYPE_CHECKING

from django.db import IntegrityError, transaction
from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from asgiref.sync import async_to_sync, sync_to_async

from automation.agent.references import merge_stored_refs
from sessions.admission import RepoAccessError, assert_repo_access
from sessions.executor.lock import SessionLock, stale_cutoff
from sessions.inbox.kinds import INBOX_TRIGGERS, PERSON_KINDS, PRECEDENCE, run_trigger_type
from sessions.inbox.policy import may_wake, report_run_limit
from sessions.inbox.render import render_request
from sessions.models import (
    EventFailure,
    EventKind,
    EventState,
    InboxEvent,
    MergeRequestOwnership,
    QuietReason,
    Run,
    RunStatus,
    Session,
    SessionOrigin,
)
from sessions.queue import aenqueue_run
from sessions.services import pinned_agent_override

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.db.models import QuerySet

logger = logging.getLogger("daiv.sessions")

DRAIN_SWEEP_LIMIT = 200


def slot_busy(session: Session) -> bool:
    """Whether a live holder has ``session``'s slot."""
    return session.active_run_id is not None and session.last_active_at >= stale_cutoff()


def job_run_active(session: Session) -> bool:
    """Whether a job run holds ``session``'s one active job place, which no run can start from the inbox before it
    ends, even one queued for a worker that has not claimed the slot yet. SYNC ONLY."""
    return Run.objects.filter(
        session=session, status__in=(RunStatus.READY, RunStatus.RUNNING), trigger_type__in=SessionOrigin.one_active()
    ).exists()


def next_runs(session: Session) -> QuerySet[Run]:
    """``session``'s QUEUED next runs, one per branch."""
    return Run.objects.filter(
        Exists(InboxEvent.objects.filter(run=OuterRef("pk"))),
        session=session,
        status=RunStatus.QUEUED,
        task_result__isnull=True,
        trigger_type__in=INBOX_TRIGGERS,
    ).order_by("created_at")


def next_run_for(session: Session, ref: str) -> Run | None:
    """``session``'s QUEUED next run for the branch ``ref``. SYNC ONLY."""
    return next_runs(session).filter(ref=ref).first()


def is_busy(session: Session) -> bool:
    """Whether a message sent to ``session`` now has to wait: a live holder has the slot, or a next run is queued
    ahead of it. SYNC ONLY."""
    return slot_busy(session) or next_runs(session).exists()


ais_busy = sync_to_async(is_busy)


def take_for_chat(thread_id: str, *, holder_id: str) -> list[InboxEvent]:
    """Take the session's pending remembered events for its branch for the chat turn that just claimed the slot
    under ``holder_id``, before its ``Run`` exists. SYNC ONLY.

    A waking event is left for the drain, which starts its run once the turn ends: only the drain renders a waking
    event's request and charges a CI attempt.
    """
    with transaction.atomic():
        session = Session.objects.select_for_update().get(pk=thread_id)
        pending = InboxEvent.objects.filter(session=session, state=EventState.PENDING, wake=False).filter(
            Q(ref="") | Q(ref=session.ref)
        )
        taken = _supersede(list(pending.order_by("created_at", "id")))
        InboxEvent.objects.filter(pk__in=[event.pk for event in taken]).update(
            state=EventState.TAKEN, claim_token=holder_id, updated_at=timezone.now()
        )
    return taken


atake_for_chat = sync_to_async(take_for_chat)


async def alink_chat_run(holder_id: str, run: Run) -> None:
    """Point the events a chat turn took under ``holder_id`` at its ``Run``, now that the row exists."""
    await InboxEvent.objects.filter(session_id=run.session_id, claim_token=holder_id, state=EventState.TAKEN).aupdate(
        run=run, claim_token=""
    )
    await (
        InboxEvent.objects
        .filter(session_id=run.session_id, state=EventState.SUPERSEDED, run__isnull=True, claim_token="")
        .filter(subject__in=InboxEvent.objects.filter(run=run).exclude(subject="").values("subject"))
        .aupdate(run=run)
    )


def drain(thread_id: str) -> Run | None:
    """Start the session's next run from its pending events, and return it; ``None`` when none started. SYNC ONLY."""
    try:
        run = _take(thread_id)
    except Session.DoesNotExist:
        return None
    if run is None:
        return None
    if async_to_sync(aenqueue_run)(run, log_context="inbox", skip_dispatch=True, inbox=True) is not None:
        logger.error(
            "inbox: could not enqueue run=%s for thread_id=%s; its events wait for the next drain", run.pk, thread_id
        )
        _undo_take(run)
        return None
    return run


adrain = sync_to_async(drain)


def drain_logged(thread_id: str) -> None:
    """:func:`drain` for callers that must not fail with it (commit hooks, signal receivers, sweeps). SYNC ONLY."""
    try:
        drain(thread_id)
    except Exception:
        logger.exception("inbox: draining thread_id=%s failed; the sweep retries it", thread_id)


def sessions_to_drain(limit: int = DRAIN_SWEEP_LIMIT) -> list[str]:
    """Sessions with a pending waking event and a free or stale slot, for the sweep. SYNC ONLY."""
    free = Q(session__active_run_id__isnull=True) | Q(session__last_active_at__lt=stale_cutoff())
    return list(
        InboxEvent.objects
        .filter(free, state=EventState.PENDING, wake=True)
        .order_by()
        .values_list("session_id", flat=True)
        .distinct()[:limit]
    )


@transaction.atomic
def _take(thread_id: str) -> Run | None:
    session = Session.objects.select_for_update().select_related("user").get(pk=thread_id)
    if slot_busy(session) or job_run_active(session):
        return None
    pending = list(
        InboxEvent.objects
        .filter(session=session, state=EventState.PENDING)
        .select_related("author")
        .order_by("created_at", "id")
    )
    queued: dict[str, Run] = {}
    for next_run in next_runs(session):
        queued.setdefault(next_run.ref, next_run)
    survivors = _supersede(pending)
    _apply_policy(session, survivors)
    waking = [event for event in survivors if event.wake]
    _drop_next_runs([run for ref, run in queued.items() if ref not in {event.ref for event in waking}])
    if not waking:
        return None

    branch = waking[0].ref
    taken = [event for event in survivors if event.ref in ("", branch)]
    if session.user is not None and not _may_run(session):
        _fail(session, taken, queued.get(branch))
        return None

    try:
        with transaction.atomic():
            run = _promote_or_create(session, branch, taken, queued.get(branch))
    except IntegrityError:
        # A job run took the session's one active place after job_run_active looked (``run_one_active_per_session``).
        logger.info("inbox: thread_id=%s has an active job run; its events wait for that run to end", thread_id)
        return None
    if not SessionLock.claim_for_run(thread_id, run.pk):
        transaction.set_rollback(True)
        return None

    InboxEvent.objects.filter(pk__in=[event.pk for event in taken]).update(
        state=EventState.TAKEN, run=run, claim_token="", updated_at=timezone.now()
    )
    InboxEvent.objects.filter(
        session=session, state=EventState.SUPERSEDED, run__isnull=True, subject__in={e.subject for e in taken} - {""}
    ).filter(Q(ref="") | Q(ref=branch)).update(run=run)
    _charge_ci_attempts(session, taken)
    _merge_references(session, taken)
    return run


def _merge_references(session: Session, taken: list[InboxEvent]) -> None:
    """Add the references the follow-up jobs the run took declared to the session, which the run reads them from."""
    declared = [
        ref for event in taken if event.kind == EventKind.FOLLOW_UP_JOB for ref in event.payload.get("references", [])
    ]
    if declared and (merged := merge_stored_refs(session.external_refs, declared)) != session.external_refs:
        Session.objects.filter(pk=session.pk).update(external_refs=merged)


def _charge_ci_attempts(session: Session, taken: list[InboxEvent]) -> None:
    """Spend an attempt of each merge request whose CI failure woke the run, and record the commit it examines."""
    for event in taken:
        if event.kind == EventKind.CI_FAILED and event.wake and event.merge_request_iid:
            MergeRequestOwnership.objects.filter(
                repo_id=session.repo_id, merge_request_iid=event.merge_request_iid
            ).update(last_examined_sha=event.payload["sha"], attempts=F("attempts") + 1)


def _supersede(pending: list[InboxEvent]) -> list[InboxEvent]:
    """Mark all but the newest pending event per subject ``superseded``, and return the rest, oldest first."""
    newest: dict[str, InboxEvent] = {}
    for event in pending:
        if event.subject:
            newest[event.subject] = event
    replaced = [event for event in pending if event.subject and newest[event.subject] is not event]
    if replaced:
        InboxEvent.objects.filter(pk__in=[event.pk for event in replaced]).update(
            state=EventState.SUPERSEDED, run=None, updated_at=timezone.now()
        )
    return [event for event in pending if event not in replaced]


def _apply_policy(session: Session, survivors: Iterable[InboxEvent]) -> None:
    """Turn the waking events the policy blocks into remembered ones, recording why."""
    limited = False
    for event in survivors:
        if event.wake and (reason := may_wake(session, event.kind)) is not None:
            event.wake, event.reason = False, reason
            event.save(update_fields=["wake", "reason", "updated_at"])
            limited = limited or reason == QuietReason.RUN_LIMIT
    if limited:
        transaction.on_commit(partial(report_run_limit, session), robust=True)


def _drop_next_runs(runs: Iterable[Run | None]) -> None:
    """Delete ``runs``, next runs left with no waking event to start them, detaching their events."""
    if ids := [run.pk for run in runs if run is not None]:
        InboxEvent.objects.filter(run_id__in=ids).update(run=None, updated_at=timezone.now())
        Run.objects.filter(pk__in=ids).delete()


def _may_run(session: Session) -> bool:
    try:
        assert_repo_access(session.user, [session.repo_id])
    except RepoAccessError:
        return False
    return True


def _fail(session: Session, taken: list[InboxEvent], next_run: Run | None) -> None:
    logger.warning(
        "inbox: the owner of thread_id=%s can no longer run on %s; %d event(s) failed",
        session.thread_id,
        session.repo_id,
        len(taken),
    )
    InboxEvent.objects.filter(pk__in=[event.pk for event in taken]).update(
        state=EventState.FAILED, failure=EventFailure.ACCESS_DENIED, run=None, updated_at=timezone.now()
    )
    _drop_next_runs([next_run])


def _promote_or_create(session: Session, branch: str, taken: list[InboxEvent], run: Run | None) -> Run:
    waking = sorted(
        (event for event in taken if event.wake), key=lambda event: (PRECEDENCE[event.kind], event.created_at)
    )
    top = waking[0]
    person = next((event for event in waking if event.kind in PERSON_KINDS), None)
    agent_model, agent_thinking_level = _agent_override(session, person)
    chats = [event for event in taken if event.kind == EventKind.CHAT_MESSAGE]
    fields = {
        "status": RunStatus.READY,
        "trigger_type": run_trigger_type(top.kind, top.payload),
        "user": person.author if person is not None and person.author_id else session.user,
        "agent_model": agent_model,
        "agent_thinking_level": agent_thinking_level,
        "message_id": chats[-1].payload["agui_message_id"] if chats else "",
        "merge_request_iid": next((event.merge_request_iid for event in waking if event.merge_request_iid), None),
        "sandbox_environment_id": session.sandbox_environment_id,
        "prompt": render_request(taken),
        "muted": person.payload.get("muted") if person is not None else None,
    }
    if run is None:
        return Run.objects.create(session=session, repo_id=session.repo_id, ref=branch, **fields)
    for name, value in fields.items():
        setattr(run, name, value)
    run.save(update_fields=list(fields))
    return run


def _agent_override(session: Session, person: InboxEvent | None) -> tuple[str, str]:
    if person is not None and person.payload.get("agent_model"):
        return person.payload["agent_model"], person.payload.get("agent_thinking_level", "")
    return pinned_agent_override(session)


def _undo_take(run: Run) -> None:
    """Free the slot claimed for ``run`` and put its events back, after its enqueue failed."""
    Session.objects.filter(thread_id=run.session_id, active_run_id=str(run.pk)).update(
        active_run_id=None, last_active_at=timezone.now()
    )
    InboxEvent.objects.filter(run=run, state=EventState.TAKEN).update(
        state=EventState.PENDING, run=None, updated_at=timezone.now()
    )
