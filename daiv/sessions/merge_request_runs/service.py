"""Where a run on a merge request goes: the session that owns the merge request, else the merge request's own.

A comment on a merge request a session owns goes to that session's inbox: a mention as a waking event, any other
comment as a remembered one, and a job (:class:`~sessions.merge_request_runs.starter.Request`) as a
``merge_request_job`` with its own run (:mod:`sessions.inbox.merge_request_jobs`). Only an owned merge request takes a
mention on a diff line. Otherwise a mention or a job starts an addressor run in the merge request's session
(:class:`~sessions.merge_request_runs.starter.RunStarter`). A trigger on an archived session starts nothing; a mention
is answered once, linking the session, and a job is told through its outcome, with nothing posted on the merge request.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.urls import reverse

from asgiref.sync import sync_to_async

from codebase.base import GitPlatform, Scope
from codebase.clients.base import PLATFORM_ERRORS, CommentThread, is_not_found
from core.utils import build_absolute_url
from sessions.admission import InvalidMcpOverridesError, aresolve_mcp_overrides
from sessions.inbox import kinds
from sessions.inbox.merge_request_jobs import ARCHIVED, MergeRequestJobRefused, MergeRequestJobs
from sessions.inbox.ownership import Ownerships
from sessions.inbox.post import Inbox
from sessions.merge_request_runs.starter import Mention, Request, RunStarter, StartOutcome
from sessions.merge_request_runs.tasks import address_merge_request_task
from sessions.models import EventKind, MergeRequestState, Run, SessionOrigin
from sessions.services import BatchSubmitFailure

if TYPE_CHECKING:
    from codebase.clients import RepoClient
    from sessions.merge_request_runs.starter import Requester
    from sessions.models import MergeRequestOwnership

logger = logging.getLogger("daiv.sessions")

NOT_FOUND = "Merge request !{iid} was not found in {repo_id}."
NOT_OPEN = "Merge request !{iid} is {state}."
UNREADABLE = "Merge request !{iid} of {repo_id} could not be read; try again."
SESSION_ARCHIVED = "The DAIV session of merge request !{iid} is archived. Unarchive it to run jobs on it: {url}"


class MergeRequestRuns:
    """Starts runs on one merge request, where a mention by the requester would run (FR-006)."""

    def __init__(self, client: RepoClient, repo_id: str, merge_request_iid: int):
        self.client = client
        self.repo_id = repo_id
        self.merge_request_iid = merge_request_iid
        self.starter = RunStarter(repo_id=repo_id, scope=Scope.MERGE_REQUEST, iid=merge_request_iid)

    async def astart(
        self,
        requester: Requester,
        input: Mention | Request,  # noqa: A002 - the contract's name for what is asked
        *,
        title: str,
        ref: str | None = None,
        use_max: bool = False,
        muted: bool | None = None,
        agent_model: str = "",
        agent_thinking_level: str = "",
        sandbox_environment_id: str | None = None,
    ) -> StartOutcome:
        """Address ``input`` in the session that owns the merge request when one does, else in the merge request's
        own session.

        ``ref`` is the merge request's source branch, ``None`` when the caller doesn't know it (GitHub's
        ``issue_comment``); it is then fetched, and left blank if that fails, since the run is still worth starting.
        ``use_max`` runs a mention on the site's max model either way: a mention for the owning session carries that
        model for the run that takes it. A job carries ``muted``, its own ``agent_model``, references and MCP switches;
        in the merge request's session it runs in ``sandbox_environment_id``, and in the owning session in the
        environment it names, else the session's.

        Raises :class:`~sessions.inbox.merge_request_jobs.MergeRequestJobRefused` with the merge request's state when a
        job finds it merged or closed in the owning session, and
        :class:`~sessions.admission.InvalidMcpOverridesError` when its MCP switches name a server the run can't load.
        """
        if (ownership := await Ownerships(self.repo_id).afind_open(self.merge_request_iid, title=title)) is not None:
            thread_id = ownership.session.thread_id
            if isinstance(input, Request):
                return await self._apost_job(ownership, requester, input, muted, agent_model, agent_thinking_level)
            if ownership.session.archived_at is not None:
                await RunStarter.areply_archived(self._reply_under(input), requester, thread_id, note_id=input.note_id)
                return StartOutcome(thread_id=thread_id, archived=True)
            created = await self._apost_comment(ownership, requester, input, kind=EventKind.MENTION, use_max=use_max)
            return StartOutcome(thread_id=thread_id, started=created)
        if isinstance(input, Mention) and input.on_diff:
            logger.info("Ignoring a diff mention on %s#%s: no session owns it", self.repo_id, self.merge_request_iid)
            return StartOutcome(thread_id=self.starter.thread_id)
        if ref is None:
            ref = await self._afetch_source_branch()
        if isinstance(input, Request):
            return await self.starter.astart(
                address_merge_request_task,
                requester,
                use_max=use_max,
                task_kwargs={
                    "merge_request_id": self.merge_request_iid,
                    "request_key": input.request_key,
                    "prompt": input.prompt,
                    "requester_username": requester.username,
                    "agent_model": agent_model or None,
                    "agent_thinking_level": agent_thinking_level or None,
                    "references": list(input.references),
                    "mcp_overrides": await self._amcp_overrides(requester, input, thread_id=None),
                },
                trigger_type=input.source,
                title=title,
                reply=None,
                ref=ref,
                prompt=input.prompt,
                muted=muted,
                agent_model=agent_model,
                agent_thinking_level=agent_thinking_level,
                sandbox_environment_id=sandbox_environment_id,
                runs_alone=True,
            )
        return await self.starter.astart(
            address_merge_request_task,
            requester,
            use_max=use_max,
            task_kwargs={
                "merge_request_id": self.merge_request_iid,
                "request_key": f"note:{input.note_id}",
                "mention_comment_id": input.comment_id,
                "mention_note_id": input.note_id,
            },
            trigger_type=SessionOrigin.MR_WEBHOOK,
            title=title,
            reply=self._reply_under(input),
            ref=ref,
        )

    async def _apost_job(
        self,
        ownership: MergeRequestOwnership,
        requester: Requester,
        request: Request,
        muted: bool | None,
        agent_model: str,
        agent_thinking_level: str,
    ) -> StartOutcome:
        """Post ``request`` as a ``merge_request_job`` in the owning session, with its own run; an archived session
        refuses it, and nothing is posted on the merge request."""
        thread_id = ownership.session.thread_id
        if ownership.session.archived_at is not None:
            logger.info("Ignoring a job on session %s: it is archived", thread_id)
            return StartOutcome(thread_id=thread_id, archived=True)
        mcp_overrides = await self._amcp_overrides(requester, request, thread_id=thread_id)
        try:
            run = await MergeRequestJobs(thread_id, self.repo_id, self.merge_request_iid).apost(
                prompt=request.prompt,
                request_key=request.request_key,
                source=request.source,
                acts_as_user=requester.acts_as_user,
                ref=ownership.source_branch,
                user=requester.user,
                username=requester.username,
                provider=requester.provider,
                uid=str(requester.uid),
                agent_model=agent_model,
                agent_thinking_level=agent_thinking_level,
                muted=muted,
                references=list(request.references),
                mcp_overrides=mcp_overrides,
                sandbox_environment_id=request.environment_id,
            )
        except MergeRequestJobRefused as refused:
            if refused.reason != ARCHIVED:
                raise
            return StartOutcome(thread_id=thread_id, archived=True)
        return StartOutcome(thread_id=thread_id, started=True, run_id=run.pk, status=run.status)

    @staticmethod
    async def _amcp_overrides(
        requester: Requester, request: Request, *, thread_id: str | None
    ) -> dict[str, str] | None:
        """``request``'s MCP switches applied to ``thread_id``'s overrides, against the servers the run may load."""
        acting = requester.user if requester.acts_as_user else None
        return await aresolve_mcp_overrides(acting, request.mcp_switches, thread_id)

    async def asubmit_job(
        self,
        requester: Requester,
        request: Request,
        *,
        muted: bool | None,
        agent_model: str = "",
        agent_thinking_level: str = "",
        sandbox_environment_id: str | None = None,
    ) -> Run | BatchSubmitFailure:
        """Start ``request`` on the merge request (:meth:`astart`), or say why it can't: the merge request is missing
        from the repository, merged or closed, its session is archived, or its MCP switches name a server it can't
        load. A refusal posts nothing on the merge request."""

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
        if merge_request.merged or merge_request.closed:
            state = MergeRequestState.MERGED if merge_request.merged else MergeRequestState.CLOSED
            return refused(NOT_OPEN.format(iid=iid, state=state.value))
        try:
            outcome = await self.astart(
                requester,
                request,
                title=merge_request.title,
                ref=merge_request.source_branch,
                muted=muted,
                agent_model=agent_model,
                agent_thinking_level=agent_thinking_level,
                sandbox_environment_id=sandbox_environment_id,
            )
        except MergeRequestJobRefused as job_refused:
            return refused(NOT_OPEN.format(iid=iid, state=job_refused.reason))
        except InvalidMcpOverridesError as err:
            return refused(str(err))
        if outcome.archived:
            url = await sync_to_async(build_absolute_url)(
                reverse("session_detail", kwargs={"thread_id": outcome.thread_id})
            )
            return refused(SESSION_ARCHIVED.format(iid=iid, url=url))
        # The job's status links its merge request before the run reports one.
        await Run.objects.filter(pk=outcome.run_id, repo_id=self.repo_id, merge_request_web_url="").aupdate(
            merge_request_web_url=merge_request.web_url or ""
        )
        return await Run.objects.aget(pk=outcome.run_id)

    async def aremember(self, requester: Requester, comment: Mention, *, title: str) -> None:
        """Remember a comment that did not mention DAIV in the session that owns the merge request; with no such
        session it concerns no one."""
        if (ownership := await Ownerships(self.repo_id).afind_open(self.merge_request_iid, title=title)) is not None:
            await self._apost_comment(ownership, requester, comment, kind=EventKind.COMMENT)

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
            author=requester.user,
            author_username=requester.username,
            author_provider=requester.provider,
            author_uid=str(requester.uid),
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
