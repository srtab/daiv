"""Where a run on a merge request goes: the session that owns the merge request, else the merge request's own.

A comment on a merge request a session owns goes to that session's inbox: a mention as a waking event, any other
comment as a remembered one, and a job (:class:`~sessions.merge_request_runs.starter.Request`,
:meth:`MergeRequestRuns.astart_job`) as a ``merge_request_job`` with its own run
(:mod:`sessions.inbox.merge_request_jobs`). Only an owned merge request takes a
mention on a diff line. Otherwise a mention or a job starts an addressor run in the merge request's session
(:class:`~sessions.merge_request_runs.starter.RunStarter`). A trigger on an archived session starts nothing; a mention
is answered once, linking the session, and a job is told through its outcome, with nothing posted on the merge request.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from asgiref.sync import sync_to_async

from codebase.base import GitPlatform, Scope
from codebase.clients.base import PLATFORM_ERRORS, CommentThread, is_not_found
from sessions.inbox import kinds
from sessions.inbox.merge_request_jobs import (
    ARCHIVED,
    MergeRequestJobRefused,
    MergeRequestJobs,
    caller_failure_message,
    state_of,
)
from sessions.inbox.ownership import Ownerships
from sessions.inbox.post import Inbox
from sessions.merge_request_runs.starter import Mention, Request, RunStarter, StartOutcome
from sessions.merge_request_runs.tasks import address_merge_request_task
from sessions.models import EventKind, Run, RunStatus, SessionOrigin
from sessions.services import BatchSubmitFailure

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from codebase.clients import RepoClient
    from sessions.merge_request_runs.starter import Requester
    from sessions.models import MergeRequestOwnership

logger = logging.getLogger("daiv.sessions")

NOT_FOUND = "Merge request !{iid} was not found in {repo_id}."
NOT_OPEN = "Merge request !{iid} is {state}."
UNREADABLE = "Merge request !{iid} of {repo_id} could not be read; try again."
SESSION_ARCHIVED = "The DAIV session of merge request !{iid} is archived. Unarchive it to run jobs on it: {url}"
NOT_STARTED = "The job on merge request !{iid} could not start."


class MergeRequestRuns:
    """Starts runs on one merge request, where a mention by the requester would run (FR-006)."""

    def __init__(self, client: RepoClient, repo_id: str, merge_request_iid: int):
        self.client = client
        self.repo_id = repo_id
        self.merge_request_iid = merge_request_iid
        self.starter = RunStarter(repo_id=repo_id, scope=Scope.MERGE_REQUEST, iid=merge_request_iid)

    async def astart(
        self, requester: Requester, mention: Mention, *, title: str, ref: str | None = None, use_max: bool = False
    ) -> StartOutcome:
        """Address ``mention`` in the session that owns the merge request when one does, else in the merge request's
        own session.

        ``ref`` is the merge request's source branch, ``None`` when the caller doesn't know it (GitHub's
        ``issue_comment``); it is then fetched, and left blank if that fails, since the run is still worth starting.
        ``use_max`` runs it on the site's max model either way: a mention for the owning session carries that model for
        the run that takes it.
        """
        if (ownership := await Ownerships(self.repo_id).afind_open(self.merge_request_iid, title=title)) is not None:
            thread_id = ownership.session.thread_id
            if ownership.session.archived_at is not None:
                await RunStarter.areply_archived(
                    self._reply_under(mention), requester, thread_id, note_id=mention.note_id
                )
                return StartOutcome(thread_id=thread_id, archived=True)
            created = await self._apost_comment(ownership, requester, mention, kind=EventKind.MENTION, use_max=use_max)
            return StartOutcome(thread_id=thread_id, started=created)
        if mention.on_diff:
            logger.info("Ignoring a diff mention on %s#%s: no session owns it", self.repo_id, self.merge_request_iid)
            return StartOutcome(thread_id=self.starter.thread_id)
        if ref is None:
            ref = await self._afetch_source_branch()
        return await self.starter.astart(
            address_merge_request_task,
            requester,
            use_max=use_max,
            task_kwargs={
                "merge_request_id": self.merge_request_iid,
                "request_key": kinds.SourceKey.note(mention.note_id),
                "mention_comment_id": mention.comment_id,
                "mention_note_id": mention.note_id,
            },
            trigger_type=SessionOrigin.MR_WEBHOOK,
            title=title,
            reply=self._reply_under(mention),
            ref=ref,
        )

    async def astart_job(
        self, requester: Requester, request: Request, *, title: str, ref: str, merge_request_web_url: str = ""
    ) -> StartOutcome:
        """Start ``request`` on the merge request's branch ``ref``: as a ``merge_request_job`` with a run of its own in
        the session that owns the merge request when one does, in the environment it names, else the session's; else
        in the merge request's own session, in the environment it was admitted with. An archived session refuses it,
        and nothing is posted on the merge request.

        Raises :class:`~sessions.inbox.merge_request_jobs.MergeRequestJobRefused` with the merge request's state when it
        finds the merge request merged or closed in the owning session.
        """
        if (ownership := await Ownerships(self.repo_id).afind_open(self.merge_request_iid, title=title)) is not None:
            return await self._apost_job(ownership, requester, request, merge_request_web_url)
        return await self.starter.astart(
            address_merge_request_task,
            requester,
            use_max=False,
            task_kwargs={
                "merge_request_id": self.merge_request_iid,
                "request_key": request.request_key,
                "prompt": request.prompt,
                "requester_username": requester.username,
                "agent_model": request.agent_model or None,
                "agent_thinking_level": request.agent_thinking_level or None,
                "references": list(request.references),
                "mcp_overrides": request.mcp_overrides,
            },
            trigger_type=request.source,
            title=title,
            reply=None,
            ref=ref,
            request=request,
            merge_request_web_url=merge_request_web_url,
        )

    async def _apost_job(
        self, ownership: MergeRequestOwnership, requester: Requester, request: Request, merge_request_web_url: str
    ) -> StartOutcome:
        thread_id = ownership.session.thread_id
        try:
            run = await MergeRequestJobs(thread_id, self.repo_id, self.merge_request_iid).apost(
                requester, request, ref=ownership.source_branch, merge_request_web_url=merge_request_web_url
            )
        except MergeRequestJobRefused as refused:
            if refused.reason != ARCHIVED:
                raise
            logger.info("Ignoring a job on session %s: it is archived", thread_id)
            return StartOutcome(thread_id=thread_id, archived=True)
        return StartOutcome(thread_id=thread_id, started=True, run=run)

    async def asubmit_job(self, requester: Requester, request: Request) -> Run | BatchSubmitFailure:
        """Start ``request`` on the merge request (:meth:`astart_job`), or say why it can't: the merge request is
        missing from the repository, merged or closed, its session is archived, or its run failed before it could
        start. A refusal posts nothing on the merge request."""

        def refused(error: str) -> BatchSubmitFailure:
            return BatchSubmitFailure(repo_id=self.repo_id, ref="", error=error)

        iid = self.merge_request_iid
        try:
            merge_request = await sync_to_async(self.client.get_merge_request)(self.repo_id, iid)
        except PLATFORM_ERRORS as err:
            if is_not_found(err):
                return refused(NOT_FOUND.format(iid=iid, repo_id=self.repo_id))
            logger.exception("Failed to read merge request %s!%s for a job", self.repo_id, iid)
            return refused(UNREADABLE.format(iid=iid, repo_id=self.repo_id))
        if not (state := state_of(merge_request)).is_open:
            return refused(NOT_OPEN.format(iid=iid, state=state.value))
        try:
            outcome = await self.astart_job(
                requester,
                request,
                title=merge_request.title,
                ref=merge_request.source_branch,
                merge_request_web_url=merge_request.web_url or "",
            )
        except MergeRequestJobRefused as job_refused:
            return refused(NOT_OPEN.format(iid=iid, state=job_refused.reason))
        if outcome.archived:
            url = await sync_to_async(RunStarter.session_url)(outcome.thread_id)
            return refused(SESSION_ARCHIVED.format(iid=iid, url=url))
        if (run := outcome.run) is None:
            return refused(NOT_STARTED.format(iid=iid))
        if run.status == RunStatus.FAILED:
            return refused(caller_failure_message(run) or NOT_STARTED.format(iid=iid))
        return run

    async def aremember(self, requester: Callable[[], Awaitable[Requester]], comment: Mention, *, title: str) -> None:
        """Remember a comment that did not mention DAIV in the session that owns the merge request, by the requester
        ``requester`` resolves once a session does; with no such session it concerns no one."""
        if (ownership := await Ownerships(self.repo_id).afind_open(self.merge_request_iid, title=title)) is not None:
            await self._apost_comment(ownership, await requester(), comment, kind=EventKind.COMMENT)

    def _reply_under(self, mention: Mention):
        thread = CommentThread(mention.comment_id, on_diff=mention.on_diff)

        def reply(body: str) -> object:
            return self.client.create_merge_request_comment(self.repo_id, self.merge_request_iid, body, reply_to=thread)

        return reply

    async def _apost_comment(
        self,
        ownership: MergeRequestOwnership,
        requester: Requester,
        comment: Mention,
        *,
        kind: str,
        use_max: bool = False,
    ) -> bool:
        """Post ``comment`` to the owning session's inbox, on the merge request's branch; returns whether it is new,
        since an edit keeps the comment's key. A remembered comment has a key of its own, so editing it to mention
        DAIV still wakes the session. With ``use_max`` the payload names the model and thinking level the drain runs
        it on."""
        review_comment = comment.on_diff and self.client.git_platform == GitPlatform.GITHUB
        payload: dict[str, Any] = {
            "body": comment.body,
            "web_url": comment.web_url,
            "discussion_id": comment.comment_id,
            "is_diff": comment.on_diff,
            ("review_comment_id" if review_comment else "note_id"): comment.note_id,
        }
        if use_max:
            payload["agent_model"], payload["agent_thinking_level"] = RunStarter.pinned_model(use_max)
        source_key = (
            kinds.SourceKey.review_comment(comment.note_id) if review_comment else kinds.SourceKey.note(comment.note_id)
        )
        result = await Inbox(ownership.session.thread_id).apost(
            kind=kind,
            source_key=source_key if kind == EventKind.MENTION else kinds.SourceKey.comment(source_key),
            payload=payload,
            wake=kind == EventKind.MENTION,
            ref=ownership.source_branch,
            merge_request_iid=ownership.merge_request_iid,
            repo_id=ownership.repo_id,
            **requester.author_fields,
        )
        return result.created

    async def _afetch_source_branch(self) -> str:
        try:
            merge_request = await sync_to_async(self.client.get_merge_request)(self.repo_id, self.merge_request_iid)
        except Exception:
            logger.exception(
                "Failed to resolve source branch for merge request %s#%s", self.repo_id, self.merge_request_iid
            )
            return ""
        return merge_request.source_branch
