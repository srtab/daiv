"""What a run on a merge request does when it can't go ahead, before its agent starts.

A mention on a merged merge request, or whose source branch is gone, is answered on the merge request, and its run ends
as its task does. A job doesn't start once its merge request was merged or closed or its session archived, checked when
its task starts and again once its run holds the session's slot, nor when its source branch is gone. Its ``Run`` then
ends FAILED, never started, with a note on the merge request saying why; an archived session posts nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.utils import timezone

from asgiref.sync import sync_to_async

from codebase.clients.base import PLATFORM_ERRORS
from sessions.inbox.merge_request_jobs import NOT_STARTED_BRANCH_GONE, MergeRequestJobs
from sessions.legs import NOT_RUN_ARCHIVED
from sessions.models import Run, RunStatus, Session
from sessions.services import aget_task_run

if TYPE_CHECKING:
    import uuid

    from codebase.base import MergeRequest
    from codebase.clients import RepoClient
    from codebase.clients.base import CommentThread
    from sessions.executor.result import AgentResult

logger = logging.getLogger("daiv.tasks")


class JobNotStarted(Exception):  # noqa: N818
    """Raised inside the run of a job that ended without starting; ``result`` is what its task returns."""

    def __init__(self, result: AgentResult):
        super().__init__(result["response"])
        self.result = result


@dataclass
class MergeRequestGuard:
    """What the guards share: the merge request they answer on, and the task's result for a run that answered without
    its agent."""

    client: RepoClient
    merge_request: MergeRequest

    async def _aanswer(self, body: str, reply_to: CommentThread | None = None) -> AgentResult:
        await sync_to_async(self.client.create_merge_request_comment)(
            self.merge_request.repo_id, self.merge_request.merge_request_id, body=body, reply_to=reply_to
        )
        return self._result(body)

    def _result(self, response: str) -> AgentResult:
        from sessions.executor.result import AgentResult

        return AgentResult(
            response=response,
            code_changes=False,
            merge_request_id=self.merge_request.merge_request_id,
            merge_request_web_url=self.merge_request.web_url,
            merge_requests=[],
            publish_errors=[],
            usage=None,
            question=None,
        )


@dataclass
class MentionGuard(MergeRequestGuard):
    """Answers a mention whose run can't go ahead: its merge request was merged, or its source branch is gone. A
    mention on the diff is answered in its thread, ``reply_to``."""

    reply_to: CommentThread | None = None

    async def askip(self) -> AgentResult | None:
        """Answer a mention on a merged merge request; ``None`` when its run goes ahead."""
        if not self.merge_request.merged:
            return None
        logger.warning(
            "Skipping MR-comment run for %s!%s: already merged.",
            self.merge_request.repo_id,
            self.merge_request.merge_request_id,
        )
        return await self._aanswer(
            f"This merge request has already been merged, so I can't act on comments left against "
            f"its branch (`{self.merge_request.source_branch}`). Please open a new issue or merge request "
            f"for any follow-up changes.",
            reply_to=self.reply_to,
        )

    async def abranch_gone(self) -> AgentResult:
        """Answer a mention whose source branch no longer exists."""
        logger.warning(
            "Skipping MR-comment run for %s!%s: source branch %r no longer exists.",
            self.merge_request.repo_id,
            self.merge_request.merge_request_id,
            self.merge_request.source_branch,
        )
        return await self._aanswer(
            f"The source branch `{self.merge_request.source_branch}` for this merge request no longer "
            f"exists, so I can't check it out to address this comment. If it was merged and deleted, "
            f"please open a new issue or merge request for any follow-up changes.",
            reply_to=self.reply_to,
        )


@dataclass(kw_only=True)
class JobGuard(MergeRequestGuard):
    """Ends a job that may not start (its merge request merged or closed, its session archived, its source branch
    gone): its task's ``Run`` FAILED, never started, with the reason as its error and as a note on the merge request,
    except for an archived session, which posts nothing. ``task_result_id`` names the task its ``Run`` is linked to."""

    thread_id: str
    task_result_id: uuid.UUID | str

    async def askip(self) -> AgentResult | None:
        """End the job when its merge request, as last read, or its session no longer lets it start; ``None`` when
        it may."""
        note = MergeRequestJobs.not_started_note(MergeRequestJobs.state_of(self.merge_request))
        if note is None and not await Session.objects.filter(pk=self.thread_id).archived().aexists():
            return None
        return await self._astop(note or NOT_RUN_ARCHIVED, note=note)

    async def arecheck(self, _ref: str) -> None:
        """``RunHooks.on_context_ready``: check again now that the run holds the session's slot, which another run may
        have held while the merge request merged or closed; raise :class:`JobNotStarted` once the job is ended."""
        await self._aread_again()
        if (result := await self.askip()) is not None:
            raise JobNotStarted(result)

    async def abranch_gone(self) -> AgentResult:
        """End the job whose source branch is gone, saying why: its merge request merged or closed, when reading it
        again says so, else the missing branch."""
        await self._aread_again()
        if (result := await self.askip()) is not None:
            return result
        return await self._astop(NOT_STARTED_BRANCH_GONE, note=NOT_STARTED_BRANCH_GONE)

    async def _aread_again(self) -> None:
        """Read the merge request again; a platform error keeps it as last read, which let the job start."""
        try:
            self.merge_request = await sync_to_async(self.client.get_merge_request)(
                self.merge_request.repo_id, self.merge_request.merge_request_id
            )
        except PLATFORM_ERRORS:
            logger.warning(
                "Could not read %s!%s again; going by it as last read",
                self.merge_request.repo_id,
                self.merge_request.merge_request_id,
                exc_info=True,
            )

    async def _astop(self, error: str, *, note: str | None) -> AgentResult:
        task_run = await aget_task_run(self.task_result_id)
        if task_run.run_id is not None:
            run = await Run.objects.aget(pk=task_run.run_id)
            await Run.objects.afinish(
                run, RunStatus.FAILED, never_started=True, error_message=error, finished_at=timezone.now()
            )
        logger.info(
            "Not starting the job on %s!%s: %s", self.merge_request.repo_id, self.merge_request.merge_request_id, error
        )
        if note is None:
            return self._result("")
        return await self._aanswer(note)
