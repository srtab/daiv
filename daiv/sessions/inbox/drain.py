"""Starting a session's next run from its pending events (``specs/001-session-inbox/contracts/inbox-service.md``).

:func:`drain` locks the session row and, unless a live holder has the slot or a job run is active, takes its pending
events in one transaction: it moves the requests that follow the session to its branch, supersedes all but the newest
event per subject, turns the CI and delegation wakes the policy blocks into remembered events, and drops a QUEUED next
run left with nothing to wake it. When a waking event survives, it picks the branch and requester of the oldest one,
checks that requester may still run on the repository (a mention is gated where it is made, FR-026), and takes their
waking events for the branch with every remembered one, up to their first builtin slash command (:func:`runs_alone`).
A command that comes first is taken alone, with nothing remembered: the slash-command middleware answers it only as
the whole request, and ends the run. The requester's next run for the branch is promoted to READY to run what was
taken, unless requests it holds stay behind; it then keeps waiting for them, and a READY run is created instead. The
drain claims the slot for that run and marks its events ``taken``. A CI failure among them spends one of its merge
request's attempts on the commit it reports in that same transaction, so a crash before the enqueue still counts it.
A denied requester's events fail, their next run ends FAILED with the ``access_denied`` label (a follow-up job's id
still resolves), and the drain goes on to the next requester. The run is enqueued once the take commits; a failed
enqueue frees the slot, puts the events back and refunds the attempt, for the sweep to retry.

A next run is a QUEUED run with an inbox trigger type, no task yet, and events linked to it. ``sessions.queue`` leaves
such runs alone: only this module starts them, because only it claims the slot for them.
"""

from __future__ import annotations

import logging
import re
import uuid
from functools import partial
from typing import TYPE_CHECKING, NamedTuple

from django.db import IntegrityError, transaction
from django.db.models import Exists, F, OuterRef, Q
from django.tasks import TaskResultStatus
from django.utils import timezone

from asgiref.sync import async_to_sync, sync_to_async
from sandbox_envs.models import SandboxEnvironment
from sandbox_envs.selection import resolve_env_for_run

from automation.agent.references import merge_stored_refs
from codebase.base import Scope
from sessions.admission import RepoAccessError, assert_repo_access
from sessions.executor.lock import SessionLock, stale_cutoff
from sessions.inbox.kinds import INBOX_TRIGGERS, PERSON_KINDS, PRECEDENCE, follows_session, run_trigger_type
from sessions.inbox.policy import quiet_reasons_now, report_run_limit
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
from slash_commands.parser import parse_slash_command
from slash_commands.registry import slash_command_registry

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.db.models import QuerySet

    from accounts.models import User

logger = logging.getLogger("daiv.sessions")

DRAIN_SWEEP_LIMIT = 200

_MENTIONED = re.compile(r"(?<![\w@])@([\w.-]+)")


def slot_busy(session: Session) -> bool:
    """Whether a live holder has ``session``'s slot."""
    return session.active_run_id is not None and session.last_active_at >= stale_cutoff()


def job_run_active(session: Session) -> bool:
    """Whether a job run holds ``session``'s one active job place, which no run can start from the inbox before it
    ends, even one queued for a worker that has not claimed the slot yet. SYNC ONLY."""
    return Run.objects.filter(
        session=session, status__in=(RunStatus.READY, RunStatus.RUNNING), trigger_type__in=SessionOrigin.one_active()
    ).exists()


class Requester(NamedTuple):
    """Whom a run is for: a DAIV user, else the git platform account of a person who never linked one."""

    user_id: int | None
    provider: str = ""
    uid: str = ""

    @classmethod
    def of_run(cls, run: Run) -> Requester:
        return cls(run.user_id, run.external_provider, run.external_uid)

    def run_lookup(self) -> dict[str, int | str | None]:
        """The ``Run`` fields that identify this requester."""
        return {"user_id": self.user_id, "external_provider": self.provider, "external_uid": self.uid}

    def run_fields(self, username: str = "") -> dict[str, int | str | None]:
        """:meth:`run_lookup` plus ``username``, which names an unlinked account."""
        return self.run_lookup() | {"external_username": "" if self.user_id is not None else username}


def branch_of(event: InboxEvent, session: Session) -> str:
    """The branch a pending ``event`` concerns now: the session's for a request that follows it
    (:func:`sessions.inbox.kinds.follows_session`), else the one it was posted on."""
    return session.ref if follows_session(event.kind, event.payload) else event.ref


def requester_user(session: Session, asked: list[InboxEvent], requester: Requester) -> User | None:
    """The DAIV user ``requester`` names: the author of one of ``asked``, else the session's owner."""
    if requester.user_id is None:
        return None
    return next((event.author for event in asked if event.author_id == requester.user_id), session.user)


def newest_of_each_subject(pending: list[InboxEvent]) -> list[InboxEvent]:
    """``pending`` less the events a newer one of the same subject replaces, oldest first."""
    newest = {event.subject: event for event in pending if event.subject}
    return [event for event in pending if not event.subject or newest[event.subject] is event]


def runs_alone(event: InboxEvent) -> bool:
    """Whether the waking person ``event``'s request, as a run renders it (``render_request``), is a builtin slash
    command, which ``SlashCommandMiddleware`` answers only when it opens a run's request, and which then ends the run.
    Inbox runs run in the GLOBAL scope (``run_job_task``).

    The bot's username is not known here, so a command after any mention counts: a wrong match costs only a run of its
    own, while a command that joined other requests would answer for all of them, or be read as prose."""
    if event.kind not in PERSON_KINDS or not event.wake:
        return False
    text = render_request([event])
    for name in set(_MENTIONED.findall(text)) or {""}:
        command = parse_slash_command(text, name)
        if command is not None and slash_command_registry.get_commands(scope=Scope.GLOBAL, command=command.command):
            return True
    return False


def split_requests(asked: list[InboxEvent]) -> list[list[InboxEvent]]:
    """``asked``, one requester's waking events for a branch oldest first, split into the runs that take them in turn:
    a builtin slash command (:func:`runs_alone`) alone, and the requests between two commands together."""
    runs: list[list[InboxEvent]] = []
    joinable = False
    for event in asked:
        alone = runs_alone(event)
        if joinable and not alone:
            runs[-1].append(event)
        else:
            runs.append([event])
        joinable = not alone
    return runs


def holding_split(splits: list[list[InboxEvent]], run_id: uuid.UUID) -> int:
    """The index of the split (:func:`split_requests`) the next run ``run_id`` serves: the last holding one of its
    events, else the first. Each split before it gets a run of its own."""
    return max((index for index, requests in enumerate(splits) if any(e.run_id == run_id for e in requests)), default=0)


def requester_of(event: InboxEvent, session: Session) -> Requester:
    """Who asked for ``event``: its author, else a person's unlinked account, else (DAIV woke on its own) the session's
    owner."""
    if event.author_id is not None:
        return Requester(event.author_id)
    if event.kind in PERSON_KINDS:
        return Requester(None, event.author_provider, event.author_uid)
    return Requester(session.user_id)


def next_runs(session: Session) -> QuerySet[Run]:
    """``session``'s QUEUED next runs, one per branch and requester."""
    return Run.objects.filter(
        Exists(InboxEvent.objects.filter(run=OuterRef("pk"))),
        session=session,
        status=RunStatus.QUEUED,
        task_result__isnull=True,
        trigger_type__in=INBOX_TRIGGERS,
    ).order_by("created_at")


def next_run_for(session: Session, ref: str, requester: Requester) -> Run | None:
    """``session``'s QUEUED next run for the branch ``ref`` and ``requester``. SYNC ONLY."""
    return next_runs(session).filter(ref=ref, **requester.run_lookup()).first()


def describe_next_run(run: Run) -> None:
    """Keep the next run's prompt and provisional trigger type in step with the requests it will take, the last of
    those waiting for it (:func:`split_requests`), so the session page shows what it will do while it waits."""
    waiting = InboxEvent.objects.filter(run=run, state=EventState.PENDING, wake=True).order_by("created_at", "id")
    requests = split_requests(list(waiting.select_related("author")))[-1]
    top = min(requests, key=lambda event: (PRECEDENCE[event.kind], event.created_at))
    Run.objects.filter(pk=run.pk).update(
        prompt=render_request(requests), trigger_type=run_trigger_type(top.kind, top.payload)
    )


def is_busy(session: Session) -> bool:
    """Whether a message sent to ``session`` now has to wait: a live holder has the slot, or a next run is queued
    ahead of it. SYNC ONLY."""
    return slot_busy(session) or next_runs(session).exists()


ais_busy = sync_to_async(is_busy)


def take_for_chat(thread_id: str, *, holder_id: str) -> list[InboxEvent]:
    """Take the session's pending remembered events for its branch for the chat turn that just claimed the slot
    under ``holder_id``, before its ``Run`` exists. SYNC ONLY.

    A waking event is left for the drain, which starts its run once the turn ends: only the drain renders a waking
    event's request and charges a CI attempt. It is still superseded by a newer event of its subject, so a turn that
    takes a passing pipeline leaves no older failure behind to wake a fix run, nor a next run with nothing to do.
    """
    with transaction.atomic():
        session = Session.objects.select_for_update().get(pk=thread_id)
        pending = list(
            InboxEvent.objects.filter(
                Q(ref="") | Q(ref=session.ref), session=session, state=EventState.PENDING
            ).order_by("created_at", "id")
        )
        survivors = _supersede(pending)
        superseded_runs = {event.run_id for event in pending if event.run_id is not None and event not in survivors}
        _drop_next_runs(
            Run.objects.filter(pk__in=superseded_runs, status=RunStatus.QUEUED).exclude(
                Exists(InboxEvent.objects.filter(run=OuterRef("pk"), state=EventState.PENDING, wake=True))
            )
        )
        taken = [event for event in survivors if not event.wake]
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
        took = _take(thread_id)
    except Session.DoesNotExist:
        return None
    if took is None:
        return None
    run, charges = took
    if async_to_sync(aenqueue_run)(run, log_context="inbox", skip_dispatch=True, inbox=True) is not None:
        logger.error(
            "inbox: could not enqueue run=%s for thread_id=%s; its events wait for the next drain", run.pk, thread_id
        )
        _undo_take(run, charges)
        return None
    return run


adrain = sync_to_async(drain)


def drain_logged(thread_id: str) -> None:
    """:func:`drain` for callers that must not fail with it (commit hooks, signal receivers, sweeps). SYNC ONLY."""
    try:
        drain(thread_id)
    except Exception:
        logger.exception("inbox: draining thread_id=%s failed; the sweep retries it", thread_id)


def keep_queued_claims_alive() -> int:
    """Heartbeat the slots the drain claimed for runs whose task still waits for a worker, which nothing else beats
    until the task starts; returns how many it beat. SYNC ONLY."""
    waiting = list(
        Run.objects.filter(status=RunStatus.READY, task_result__status=TaskResultStatus.READY).values_list(
            "pk", "session_id"
        )
    )
    return Session.objects.filter(
        thread_id__in={session_id for _, session_id in waiting}, active_run_id__in=[str(pk) for pk, _ in waiting]
    ).update(last_active_at=timezone.now())


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


class _Charge(NamedTuple):
    """A CI attempt the take spent: the merge request, the commit it recorded as examined, the failure that woke."""

    merge_request_iid: int
    sha: str
    event_id: uuid.UUID


@transaction.atomic
def _take(thread_id: str) -> tuple[Run, list[_Charge]] | None:
    session = Session.objects.select_for_update(of=("self",)).select_related("user").get(pk=thread_id)
    if slot_busy(session) or job_run_active(session):
        return None
    pending = list(
        InboxEvent.objects
        .filter(session=session, state=EventState.PENDING)
        .select_related("author")
        .order_by("created_at", "id")
    )
    queued_runs = list(next_runs(session))
    _follow_session_branch(session, pending, queued_runs)
    queued: dict[tuple[str, Requester], Run] = {}
    for next_run in queued_runs:
        queued.setdefault((next_run.ref, Requester.of_run(next_run)), next_run)
    survivors = _supersede(pending)
    _apply_policy(session, survivors)
    waking = [event for event in survivors if event.wake]
    wanted = {(event.ref, requester_of(event, session)) for event in waking}
    _drop_next_runs([run for key, run in queued.items() if key not in wanted])
    if not waking:
        return None

    branch, requester = waking[0].ref, requester_of(waking[0], session)
    requested = [event for event in waking if event.ref in ("", branch) and requester_of(event, session) == requester]
    splits = split_requests(requested)
    asked = splits[0]
    if runs_alone(asked[0]):
        taken = asked
    else:
        taken = [event for event in survivors if event.ref in ("", branch) and (not event.wake or event in asked)]
    waiting_run = queued.get((branch, requester))
    next_run = waiting_run if waiting_run is not None and holding_split(splits, waiting_run.pk) == 0 else None
    user = requester_user(session, requested, requester)
    checked = any(event.kind != EventKind.MENTION for event in requested)
    if user is not None and checked and not _may_run(user, session.repo_id):
        _fail(session, requested, waiting_run)
        _drop_emptied(queued_runs)
        transaction.on_commit(partial(drain_logged, thread_id), robust=True)
        return None

    env_id = _sandbox_env_id(session, user, taken)
    try:
        with transaction.atomic():
            run = _promote_or_create(
                session, branch, taken, next_run, requester=requester, sandbox_environment_id=env_id
            )
    except IntegrityError:
        # A job run took the session's one active place after job_run_active looked (``run_one_active_per_session``).
        logger.info("inbox: thread_id=%s has an active job run; its events wait for that run to end", thread_id)
        _drop_emptied(queued_runs)
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
    _drop_emptied(queued_run for queued_run in queued_runs if queued_run.pk != run.pk)
    if waiting_run is not None and waiting_run.pk != run.pk:
        describe_next_run(waiting_run)
    _merge_references(session, taken)
    _apply_tools_selection(session, taken)
    return run, _charge_ci_attempts(session, taken)


def _follow_session_branch(session: Session, pending: list[InboxEvent], queued_runs: list[Run]) -> None:
    """Point the pending requests that follow the session (:func:`sessions.inbox.kinds.follows_session`) at its branch,
    which the run they waited for may have moved by publishing to a new one. A next run that only such requests wait
    in moves with them, keeping the id a follow-up job's caller polls; the others let them go."""
    moved = [event for event in pending if branch_of(event, session) != event.ref]
    if not moved:
        return
    held = {(run.ref, Requester.of_run(run)) for run in queued_runs}
    kept = set()
    for run in queued_runs:
        riders = [event for event in pending if event.run_id == run.pk]
        key = (session.ref, Requester.of_run(run))
        if riders and all(event in moved for event in riders) and key not in held and _move(run, session.ref):
            held.add(key)
            kept.add(run.pk)
    InboxEvent.objects.filter(pk__in=[event.pk for event in moved]).update(ref=session.ref, updated_at=timezone.now())
    InboxEvent.objects.filter(pk__in=[event.pk for event in moved if event.run_id not in kept]).update(run=None)
    for event in moved:
        event.ref = session.ref
        if event.run_id not in kept:
            event.run_id = None


def _move(run: Run, ref: str) -> bool:
    """Move the next run ``run`` to the branch ``ref``, unless a QUEUED run already holds its place there."""
    try:
        with transaction.atomic():
            Run.objects.filter(pk=run.pk).update(ref=ref)
    except IntegrityError:
        return False
    run.ref = ref
    return True


def _merge_references(session: Session, taken: list[InboxEvent]) -> None:
    """Add the references the follow-up jobs the run took declared to the session, which the run reads them from."""
    declared = [
        ref for event in taken if event.kind == EventKind.FOLLOW_UP_JOB for ref in event.payload.get("references", [])
    ]
    if declared and (merged := merge_stored_refs(session.external_refs, declared)) != session.external_refs:
        Session.objects.filter(pk=session.pk).update(external_refs=merged)


def _apply_tools_selection(session: Session, taken: list[InboxEvent]) -> None:
    """Give the session the Tools selection the newest chat message the run took was sent with, which ``run_job_task``
    reads when the run starts."""
    selections = [event.payload["mcp_overrides"] for event in taken if "mcp_overrides" in event.payload]
    if selections and selections[-1] != session.mcp_overrides:
        Session.objects.filter(pk=session.pk).update(mcp_overrides=selections[-1])


def _charge_ci_attempts(session: Session, taken: list[InboxEvent]) -> list[_Charge]:
    """Spend an attempt of each merge request whose CI failure woke the run taking ``taken``, and record the commit it
    examines; return the charges. A run that takes the failure again after a failed one examines the same commit, and
    is not charged again."""
    charges = []
    for event in taken:
        if event.kind != EventKind.CI_FAILED or not event.wake or event.merge_request_iid is None:
            continue
        sha = event.payload["sha"]
        rows = MergeRequestOwnership.objects.filter(repo_id=session.repo_id, merge_request_iid=event.merge_request_iid)
        if rows.exclude(last_examined_sha=sha).update(last_examined_sha=sha, attempts=F("attempts") + 1):
            charges.append(_Charge(event.merge_request_iid, sha, event.pk))
    return charges


def _supersede(pending: list[InboxEvent]) -> list[InboxEvent]:
    """Mark all but the newest pending event per subject ``superseded``, and return the rest, oldest first."""
    survivors = newest_of_each_subject(pending)
    if replaced := [event for event in pending if event not in survivors]:
        InboxEvent.objects.filter(pk__in=[event.pk for event in replaced]).update(
            state=EventState.SUPERSEDED, run=None, updated_at=timezone.now()
        )
    return survivors


def _apply_policy(session: Session, survivors: list[InboxEvent]) -> None:
    """Turn the waking events the policy blocks into remembered ones, recording why. A CI failure whose merge request
    was closed, or whose watch was turned off, since it was posted no longer wakes either."""
    reasons = quiet_reasons_now(session, survivors)
    for event in survivors:
        if (reason := reasons.get(event.pk)) is not None:
            event.wake, event.reason = False, reason
            event.save(update_fields=["wake", "reason", "updated_at"])
    if QuietReason.RUN_LIMIT in reasons.values():
        transaction.on_commit(partial(report_run_limit, session), robust=True)


def _drop_next_runs(runs: Iterable[Run | None]) -> None:
    """Delete ``runs``, next runs left with no waking event to start them, detaching their events."""
    if ids := [run.pk for run in runs if run is not None]:
        InboxEvent.objects.filter(run_id__in=ids).update(run=None, updated_at=timezone.now())
        Run.objects.filter(pk__in=ids).delete()


def _drop_emptied(runs: Iterable[Run]) -> None:
    """Delete the next runs among ``runs`` the take left with no event, whose requests went to another run (one sent
    without a branch matches any); ``sessions.queue`` would otherwise start them as plain jobs."""
    _drop_next_runs(
        Run.objects.filter(pk__in=[run.pk for run in runs], status=RunStatus.QUEUED).exclude(
            Exists(InboxEvent.objects.filter(run=OuterRef("pk")))
        )
    )


def _may_run(user: User, repo_id: str) -> bool:
    try:
        assert_repo_access(user, [repo_id])
    except RepoAccessError:
        return False
    return True


def _fail(session: Session, asked: list[InboxEvent], next_run: Run | None) -> None:
    """Fail the events of a requester who may no longer run on the repository, and finish their QUEUED next run
    FAILED with the ``access_denied`` label, so the id a follow-up job's caller polls still resolves.

    The finish says ``skip_dispatch``: the caller drains once this commits, and its events, already failed rather
    than taken, are left as they are by the run's end (:func:`sessions.inbox.complete.settle_events`)."""
    logger.warning(
        "inbox: a requester on thread_id=%s may no longer run on %s; %d event(s) failed",
        session.thread_id,
        session.repo_id,
        len(asked),
    )
    now = timezone.now()
    InboxEvent.objects.filter(pk__in=[event.pk for event in asked]).update(
        state=EventState.FAILED, failure=EventFailure.ACCESS_DENIED, run=None, updated_at=now
    )
    if next_run is None:
        return
    InboxEvent.objects.filter(run=next_run).update(run=None, updated_at=now)
    Run.objects.finish(
        next_run,
        RunStatus.FAILED,
        skip_dispatch=True,
        never_started=True,
        error_message=str(EventFailure.ACCESS_DENIED.label),
        finished_at=now,
        started_at=now,
    )


def _sandbox_env_id(session: Session, user: User | None, taken: list[InboxEvent]) -> uuid.UUID | None:
    """The environment the newest follow-up job among ``taken`` was submitted with, while ``user`` still sees it. Else
    the session's, unless it is a USER environment ``user`` does not own: a run for another person, or for no one,
    never gets the owner's secrets. Then the one the repository resolves to for ``user``."""
    visible = SandboxEnvironment.objects.visible_to(user)  # ty: ignore[unresolved-attribute]
    asked = [
        event.payload["sandbox_environment_id"]
        for event in taken
        if event.kind == EventKind.FOLLOW_UP_JOB and event.payload.get("sandbox_environment_id")
    ]
    if asked and visible.filter(pk=asked[-1]).exists():
        return uuid.UUID(asked[-1])
    env_id = session.sandbox_environment_id
    if env_id is None or visible.filter(pk=env_id).exists():
        return env_id
    env = async_to_sync(resolve_env_for_run)(user=user, repo_id=session.repo_id)
    return None if env is None else env.pk


def _promote_or_create(
    session: Session,
    branch: str,
    taken: list[InboxEvent],
    run: Run | None,
    *,
    requester: Requester,
    sandbox_environment_id: uuid.UUID | None,
) -> Run:
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
        **requester.run_fields(person.author_username if person is not None else ""),
        "agent_model": agent_model,
        "agent_thinking_level": agent_thinking_level,
        "message_id": chats[-1].payload["agui_message_id"] if chats else "",
        "merge_request_iid": next((event.merge_request_iid for event in waking if event.merge_request_iid), None),
        "sandbox_environment_id": sandbox_environment_id,
        "prompt": render_request(taken),
        "muted": (person or top).payload.get("muted"),
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


def _undo_take(run: Run, charges: list[_Charge]) -> None:
    """Free the slot claimed for ``run``, put its events back and refund the CI attempts its take spent, after its
    enqueue failed. The slot and events are usually back already: the failed run's ``run_ended`` did both
    (:func:`sessions.inbox.complete.on_run_ended`), unless its finish did not land.

    A refund leaves the merge request's examined commit blank, as the one before is not kept. That commit is older than
    the failure's, so it is no longer the head DAIV pushed, the only commit whose failure wakes the session. A failure
    another drain took after ``run_ended`` put it back keeps its charge: that run did not charge the commit again. The
    session row lock orders the check against such a take.
    """
    with transaction.atomic():
        Session.objects.select_for_update().filter(pk=run.session_id).exists()
        SessionLock.release_for_run(run.session_id, run.pk)
        InboxEvent.objects.filter(run=run, state=EventState.TAKEN).update(
            state=EventState.PENDING, run=None, updated_at=timezone.now()
        )
        retaken = set(
            InboxEvent.objects.filter(
                pk__in=[charge.event_id for charge in charges], state=EventState.TAKEN
            ).values_list("pk", flat=True)
        )
        for charge in charges:
            if charge.event_id in retaken:
                continue
            rows = MergeRequestOwnership.objects.filter(repo_id=run.repo_id, merge_request_iid=charge.merge_request_iid)
            rows.filter(attempts__gt=0).update(attempts=F("attempts") - 1)
            rows.filter(last_examined_sha=charge.sha).update(last_examined_sha="")
