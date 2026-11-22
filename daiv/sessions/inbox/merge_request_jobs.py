"""A job on a merge request a session owns: a ``merge_request_job`` inbox event posted with a QUEUED run of its own.

A job sent through the jobs API or MCP that names a merge request a session owns runs where a mention by its caller
would, as one turn of that session (``sessions.merge_request_runs.service``). Its run carries the requester, so spend
and CI identity polling find it, and ``runs_alone``, so it is never another event's next run: each job keeps its own
id and status. The event has no subject, so a newer job never supersedes an older one. The drain takes it alone, with
the branch's remembered events (:mod:`sessions.inbox.splits`), and promotes its run to READY.

The event ends with its run and is never retried, since the job reports that one run
(:meth:`MergeRequestJobs.end_with_run`). It stops at its start, without running, when its merge request was merged or
closed after it was posted, saying so on the merge request once the stop commits (:meth:`MergeRequestJobs.stop`), and
as soon as the drain sees the session archived (:meth:`MergeRequestJobs.stop_held`), posting nothing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from asgiref.sync import sync_to_async

from sessions.legs import NOT_RUN_ARCHIVED
from sessions.live.notify import session_changed
from sessions.models import (
    EventFailure,
    EventKind,
    EventState,
    InboxEvent,
    MergeRequestOwnership,
    MergeRequestState,
    Run,
    RunStatus,
    Session,
)

if TYPE_CHECKING:
    from accounts.models import User

logger = logging.getLogger("daiv.sessions")

ARCHIVED = "archived"
NOT_STARTED_MERGED = "This merge request was merged before the job could start, so I didn't run it."
NOT_STARTED_CLOSED = "This merge request was closed before the job could start, so I didn't run it."
#: Why a merge request job ended FAILED without starting; safe to show its caller.
NOT_STARTED_MESSAGES = frozenset({NOT_STARTED_MERGED, NOT_STARTED_CLOSED, NOT_RUN_ARCHIVED})

_STOPS = {
    MergeRequestState.MERGED: (EventFailure.MR_MERGED, NOT_STARTED_MERGED),
    MergeRequestState.CLOSED: (EventFailure.MR_CLOSED, NOT_STARTED_CLOSED),
}
_NOT_STARTED = {**dict(_STOPS.values()), EventFailure.SESSION_ARCHIVED: NOT_RUN_ARCHIVED}


class MergeRequestJobRefused(Exception):  # noqa: N818
    """The job was not posted: ``reason`` is :data:`ARCHIVED`, or the ``MergeRequestState`` of its merge request,
    merged or closed."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class MergeRequestJobs:
    """The jobs on merge request ``merge_request_iid`` of ``repo_id``, which the session ``thread_id`` owns."""

    def __init__(self, thread_id: str, repo_id: str, merge_request_iid: int):
        self.thread_id = thread_id
        self.repo_id = repo_id
        self.merge_request_iid = merge_request_iid

    def post(
        self,
        *,
        prompt: str,
        request_key: str,
        source: str,
        acts_as_user: bool,
        ref: str,
        user: User | None,
        username: str = "",
        provider: str = "",
        uid: str = "",
        agent_model: str = "",
        agent_thinking_level: str = "",
        muted: bool | None = None,
        references: list[dict] | None = None,
        mcp_overrides: dict[str, str] | None = None,
        sandbox_environment_id: str | None = None,
    ) -> Run:
        """Create the ``merge_request_job`` event and its QUEUED run in one transaction, with the session row locked,
        drain the session once that commits, and return the run as the drain left it. ``user`` is the requester, else
        the platform account ``provider``/``uid``/``username`` is. ``references``, ``mcp_overrides`` and
        ``sandbox_environment_id`` reach the run as a follow-up job's do (:mod:`sessions.inbox.drain`). A merge request
        of another of the session's repositories is posted as that repository's event, whose run is on the session's
        branch. Raises :class:`MergeRequestJobRefused` when, under that lock, the session is archived or the merge
        request is no longer open. SYNC ONLY."""
        from sessions.inbox.drain import Drain

        with transaction.atomic():
            session = Session.objects.select_for_update().get(pk=self.thread_id)
            if session.archived_at is not None:
                raise MergeRequestJobRefused(ARCHIVED)
            if not (state := self._state()).is_open:
                raise MergeRequestJobRefused(state)
            payload: dict = {"prompt": prompt, "acts_as_user": acts_as_user, "source": source}
            if agent_model:
                payload |= {"agent_model": agent_model, "agent_thinking_level": agent_thinking_level}
            if muted is not None:
                payload["muted"] = muted
            if references:
                payload["references"] = references
            if mcp_overrides is not None:
                payload["mcp_overrides"] = mcp_overrides
            if sandbox_environment_id:
                payload["sandbox_environment_id"] = sandbox_environment_id
            other_repo = self.repo_id != session.repo_id
            run = Run.objects.create(
                session=session,
                trigger_type=source,
                status=RunStatus.QUEUED,
                runs_alone=True,
                user=user,
                external_username=username,
                external_provider=provider,
                external_uid=uid,
                acts_as_user=acts_as_user,
                muted=muted,
                prompt=prompt,
                repo_id=session.repo_id,
                ref=session.ref if other_repo else ref,
                merge_request_iid=None if other_repo else self.merge_request_iid,
                agent_model=agent_model,
                agent_thinking_level=agent_thinking_level,
                sandbox_environment_id=session.sandbox_environment_id,
            )
            InboxEvent.objects.create(
                session=session,
                kind=EventKind.MERGE_REQUEST_JOB,
                source_key=request_key,
                payload=payload,
                wake=True,
                ref=ref,
                repo_id=self.repo_id if other_repo else "",
                merge_request_iid=self.merge_request_iid,
                author=user,
                author_username=username,
                author_provider=provider,
                author_uid=uid,
                run=run,
            )
            session_changed(session.thread_id)
            transaction.on_commit(Drain(session.thread_id).start_logged, robust=True)
        run.refresh_from_db(fields=["status"])
        return run

    async def apost(self, **kwargs) -> Run:
        return await sync_to_async(self.post)(**kwargs)

    def _state(self) -> MergeRequestState:
        key = (self.repo_id, self.merge_request_iid)
        return MergeRequestOwnership.states_of([key])[key]

    @staticmethod
    def stop_reason(session: Session, event: InboxEvent) -> EventFailure | None:
        """Why the job ``event`` on ``session`` must not start: its session was archived, or its merge request was
        merged or closed."""
        if session.archived_at is not None:
            return EventFailure.SESSION_ARCHIVED
        key = (event.effective_repo_id(session.repo_id), event.merge_request_iid)
        stop = _STOPS.get(MergeRequestOwnership.states_of([key])[key])
        return None if stop is None else stop[0]

    @staticmethod
    def stop(event: InboxEvent, failure: EventFailure, run: Run) -> None:
        """Fail the job ``event`` with ``failure`` and finish its ``run`` FAILED, never started, with why; a merged or
        closed merge request is told so once this commits. The finish says ``skip_dispatch``: the caller drains once
        this commits."""
        now = timezone.now()
        InboxEvent.objects.filter(pk=event.pk).update(state=EventState.FAILED, failure=failure, updated_at=now)
        session_changed(event.session_id)
        note = _NOT_STARTED[failure]
        Run.objects.finish(
            run,
            RunStatus.FAILED,
            skip_dispatch=True,
            never_started=True,
            error_message=note,
            finished_at=now,
            started_at=now,
        )
        if failure != EventFailure.SESSION_ARCHIVED:
            repo_id, iid = event.effective_repo_id(run.repo_id), event.merge_request_iid
            transaction.on_commit(lambda: MergeRequestJobs._comment(repo_id, iid, note), robust=True)

    @staticmethod
    def stop_held(session: Session) -> None:
        """Stop the pending jobs of the archived ``session``, whose drain holds every event, so each job ends. SYNC
        ONLY, with the session row locked."""
        held = InboxEvent.objects.filter(session=session, state=EventState.PENDING, kind=EventKind.MERGE_REQUEST_JOB)
        for event in held:
            run = Run.objects.select_for_update().filter(pk=event.run_id).first()
            if run is None or run.status != RunStatus.QUEUED:
                MergeRequestJobs.settle_unstartable(event, run)
            else:
                MergeRequestJobs.stop(event, EventFailure.SESSION_ARCHIVED, run)

    @staticmethod
    def settle_unstartable(event: InboxEvent, run: Run | None) -> None:
        """Settle the pending job ``event`` whose own ``run`` is gone or no longer QUEUED, so the drain moves on: as
        :meth:`end_with_run` settles it when that run failed, else failed (``run_failed``). SYNC ONLY."""
        if run is not None and run.status == RunStatus.FAILED:
            MergeRequestJobs.end_with_run(run)
        elif InboxEvent.objects.filter(pk=event.pk, state=EventState.PENDING).update(
            state=EventState.FAILED, failure=EventFailure.RUN_FAILED, updated_at=timezone.now()
        ):
            session_changed(event.session_id)

    @staticmethod
    def end_with_run(run: Run) -> list[InboxEvent]:
        """Settle the job linked to ``run`` once it ended FAILED, whether the drain took it or not: dismissed when the
        run was cancelled, else failed (``run_failed``). Returns the jobs it failed, whose merge request is owed a note;
        a run that succeeded leaves its taken job to the shared path, which marks it handled. SYNC ONLY."""
        if run.status != RunStatus.FAILED:
            return []
        jobs = InboxEvent.objects.filter(
            run=run, kind=EventKind.MERGE_REQUEST_JOB, state__in=(EventState.PENDING, EventState.TAKEN)
        )
        now = timezone.now()
        if run.is_cancelled:
            if jobs.update(state=EventState.DISMISSED, updated_at=now):
                session_changed(run.session_id)
            return []
        failed = list(jobs)
        if jobs.update(state=EventState.FAILED, failure=EventFailure.RUN_FAILED, updated_at=now):
            session_changed(run.session_id)
        return failed

    @staticmethod
    def _comment(repo_id: str, merge_request_iid: int, body: str) -> None:
        from codebase.clients import RepoClient

        try:
            RepoClient.create_instance().create_merge_request_comment(repo_id, merge_request_iid, body)
        except Exception:
            logger.exception("inbox: could not comment on %s!%s", repo_id, merge_request_iid)
