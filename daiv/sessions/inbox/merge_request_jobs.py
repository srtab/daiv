"""A job on a merge request a session owns: a ``merge_request_job`` inbox event posted with a QUEUED run of its own.

A job sent through the jobs API or MCP that names a merge request a session owns runs where a mention by its caller
would, as one turn of that session (``sessions.merge_request_runs.service``). Its run carries the caller, so spend
and CI identity polling find it, and ``runs_alone``, so it is never another event's next run: each job keeps its own
id and status. The event has no subject, so a newer job never supersedes an older one. The drain takes it alone, with
the branch's remembered events (:mod:`sessions.inbox.splits`), and promotes its run to READY.

The job's MCP switches and references apply to its run only (:meth:`MergeRequestJobs.run_options`): the session's
Tools selection and references, which may be another person's, stay as they are.

The event ends with its run and is never retried, since the job reports that one run (:mod:`sessions.inbox.own_runs`).
A failed run is told on the merge request, with the provider's failure or the restart that interrupted it. A job stops
at its start, without running, when its merge request was merged or closed after it was posted, or its requester may no
longer run on the session's repositories, saying so on the merge request once the stop commits, and as soon as the drain
sees the session archived, posting nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, NamedTuple, override

from django.db import transaction

from asgiref.sync import sync_to_async
from mcp_connectors.selection import build_selection_pool, overrides_from_switches

from automation.agent.references import merge_stored_refs, refs_from_stored
from core.constants import BOT_NAME
from sessions.inbox.own_runs import MERGE_REQUEST_STOPS, OwnRunEvents
from sessions.legs import NOT_RUN_ARCHIVED
from sessions.live.notify import session_changed
from sessions.models import (
    EventFailure,
    EventKind,
    InboxEvent,
    MergeRequestOwnership,
    MergeRequestState,
    Run,
    RunStatus,
    Session,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from automation.agent.references import ExternalRef
    from codebase.base import MergeRequest
    from sessions.inbox.complete import MergeRequestReplies
    from sessions.merge_request_runs.starter import Caller, Request

ARCHIVED = "archived"
NOT_STARTED_MERGED = "This merge request was merged before the job could start, so I didn't run it."
NOT_STARTED_CLOSED = "This merge request was closed before the job could start, so I didn't run it."
NOT_STARTED_ACCESS_DENIED = (
    "The person this job would run as can't run on every repository its session works on, so I didn't run it."
)
NOT_STARTED_BRANCH_GONE = "The source branch of this merge request no longer exists, so I didn't run the job."

INTERRUPTED_NOTE = f"{BOT_NAME} was restarted while working on this job, so the job stopped before it finished."


class MergeRequestJobRefused(Exception):  # noqa: N818
    """The job was not posted: ``reason`` is :data:`ARCHIVED`, or the ``MergeRequestState`` of its merge request,
    merged or closed."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class MergeRequestJobs(OwnRunEvents):
    """The jobs on merge request ``merge_request_iid`` of ``repo_id``, which the session ``thread_id`` owns."""

    kind = EventKind.MERGE_REQUEST_JOB
    not_run: ClassVar[dict[EventFailure, str]] = {
        EventFailure.MR_MERGED: NOT_STARTED_MERGED,
        EventFailure.MR_CLOSED: NOT_STARTED_CLOSED,
        EventFailure.SESSION_ARCHIVED: NOT_RUN_ARCHIVED,
        EventFailure.ACCESS_DENIED: NOT_STARTED_ACCESS_DENIED,
    }

    def __init__(self, thread_id: str, repo_id: str, merge_request_iid: int):
        self.thread_id = thread_id
        self.repo_id = repo_id
        self.merge_request_iid = merge_request_iid

    def post(self, caller: Caller, request: Request, *, ref: str, merge_request_web_url: str = "") -> Run:
        """Create the ``merge_request_job`` event and its QUEUED run for ``caller`` in one transaction, with the
        session row locked, drain the session once that commits, and return the run as the drain left it. The job's
        named environment reaches its run as a follow-up job's does (:mod:`sessions.inbox.drain`), and its MCP switches
        and references as :meth:`run_options` applies them. A merge request of another of the session's repositories
        is posted as that repository's event, whose run is on the session's branch. Raises
        :class:`MergeRequestJobRefused` when, under that lock, the session is archived or the merge request is no longer
        open. SYNC ONLY."""
        from sessions.inbox.drain import Drain

        with transaction.atomic():
            session = Session.objects.select_for_update().get(pk=self.thread_id)
            if session.archived_at is not None:
                raise MergeRequestJobRefused(ARCHIVED)
            if not (state := self._state(self.repo_id, self.merge_request_iid)).is_open:
                raise MergeRequestJobRefused(state)
            payload: dict = {"prompt": request.prompt, "trigger_type": request.trigger_type}
            if request.agent_model:
                payload |= {"agent_model": request.agent_model, "agent_thinking_level": request.agent_thinking_level}
            if request.muted is not None:
                payload["muted"] = request.muted
            if request.references:
                payload["references"] = list(request.references)
            if request.mcp_switches:
                payload["mcp_switches"] = request.mcp_switches
            if request.named_environment_id:
                payload["sandbox_environment_id"] = request.named_environment_id
            other_repo = self.repo_id != session.repo_id
            run = Run.objects.create(
                session=session,
                trigger_type=request.trigger_type,
                status=RunStatus.QUEUED,
                runs_alone=True,
                **caller.run_fields,
                muted=request.muted,
                prompt=request.prompt,
                repo_id=session.repo_id,
                ref=session.ref if other_repo else ref,
                merge_request_iid=None if other_repo else self.merge_request_iid,
                merge_request_web_url="" if other_repo else merge_request_web_url,
                agent_model=request.agent_model,
                agent_thinking_level=request.agent_thinking_level,
                sandbox_environment_id=session.sandbox_environment_id,
            )
            InboxEvent.objects.create(
                session=session,
                kind=EventKind.MERGE_REQUEST_JOB,
                source_key=request.request_key,
                payload=payload,
                wake=True,
                ref=ref,
                repo_id=self.repo_id if other_repo else "",
                merge_request_iid=self.merge_request_iid,
                **caller.author_fields,
                run=run,
            )
            session_changed(session.thread_id)
            transaction.on_commit(Drain(session.thread_id).start_logged, robust=True)
        run.refresh_from_db()
        return run

    async def apost(self, *args, **kwargs) -> Run:
        return await sync_to_async(self.post)(*args, **kwargs)

    @staticmethod
    def state_of(merge_request: MergeRequest) -> MergeRequestState:
        """The state of ``merge_request`` as the platform reports it."""
        if merge_request.merged:
            return MergeRequestState.MERGED
        if merge_request.closed:
            return MergeRequestState.CLOSED
        return MergeRequestState.DRAFT if merge_request.draft else MergeRequestState.OPEN

    @classmethod
    def not_started_note(cls, state: MergeRequestState) -> str | None:
        """Why a job doesn't start on a merge request in ``state``, said on it; ``None`` while it is open."""
        failure = MERGE_REQUEST_STOPS.get(state)
        return None if failure is None else cls.not_run[failure]

    @classmethod
    def caller_failure_message(cls, run: Run) -> str | None:
        """Why the FAILED ``run`` failed, when it is safe to tell the job's caller: the provider's failure, or why a job
        never started."""
        not_started = run.error_message in (*cls.not_run.values(), NOT_STARTED_BRANCH_GONE)
        return run.provider_failure_message() or (run.error_message if not_started else None)

    @staticmethod
    def _state(repo_id: str, merge_request_iid: int) -> MergeRequestState:
        key = (repo_id, merge_request_iid)
        return MergeRequestOwnership.states_of([key])[key]

    @classmethod
    @override
    def merge_request_state(cls, session: Session, event: InboxEvent) -> MergeRequestState:
        return cls._state(event.effective_repo_id(session.repo_id), event.merge_request_iid)

    @staticmethod
    def run_options(events: Iterable[InboxEvent], session: Session, *, acting_user_id: int | None) -> RunOptions:
        """The Tools selection and references of a run that took ``events`` in ``session``: the session's, with the MCP
        switches and references of a merge request job among them applied, against the servers the run's acting user
        may load. A switch for a server removed since the job was admitted is skipped. SYNC ONLY."""
        job = next((event for event in events if event.kind == EventKind.MERGE_REQUEST_JOB), None)
        if job is None:
            return RunOptions(session.mcp_overrides, session.external_references())
        mcp_overrides = session.mcp_overrides
        if switches := job.payload.get("mcp_switches"):
            pool = build_selection_pool(acting_user_id)
            loadable = {entry.name for entry in pool}
            mcp_overrides = overrides_from_switches(
                {name: on for name, on in switches.items() if name in loadable}, pool, mcp_overrides
            )
        references = merge_stored_refs(session.external_refs, job.payload.get("references", []))
        return RunOptions(mcp_overrides, refs_from_stored(references))

    @classmethod
    @override
    def _stopped(cls, event: InboxEvent, failure: EventFailure, run: Run) -> None:
        if failure != EventFailure.SESSION_ARCHIVED:
            note = cls.not_run[failure]
            cls._comment_once_committed(run, [event], lambda _replies: note)

    @classmethod
    @override
    def _failed(cls, run: Run, events: list[InboxEvent]) -> None:
        note = cls._failure_note(run)
        cls._comment_once_committed(run, events, lambda replies: replies.unable_note(failure_message=note))

    @staticmethod
    def _failure_note(run: Run) -> str | None:
        """Why the job's ``run`` failed, said on its merge request in place of the generic first line: the provider's
        failure, or the restart that interrupted it; ``None`` for any other failure."""
        return run.provider_failure_message() or (INTERRUPTED_NOTE if run.is_interrupted else None)

    @staticmethod
    def _comment_once_committed(run: Run, events: list[InboxEvent], body: Callable[[MergeRequestReplies], str]) -> None:
        from sessions.inbox.complete import MergeRequestReplies

        transaction.on_commit(lambda: MergeRequestReplies.comment_on_events(events, run, body), robust=True)


class RunOptions(NamedTuple):
    mcp_overrides: dict[str, str]
    references: tuple[ExternalRef, ...]
