from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from django.tasks import task
from django.utils import timezone

from codebase.clients import RepoClient
from codebase.exceptions import CloneRefNotFoundError
from sessions.inbox.merge_request_jobs import NOT_STARTED_CLOSED, NOT_STARTED_MERGED
from sessions.legs import NOT_RUN_ARCHIVED
from sessions.models import Run, RunStatus, Session
from sessions.services import aget_task_run

if TYPE_CHECKING:
    from django.tasks import TaskContext

    from codebase.base import MergeRequest
    from sessions.executor.result import AgentResult

logger = logging.getLogger("daiv.tasks")


def _skip_result(response: str, merge_request: MergeRequest) -> AgentResult:
    from sessions.executor.result import AgentResult

    return AgentResult(
        response=response,
        code_changes=False,
        merge_request_id=merge_request.merge_request_id,
        merge_request_web_url=merge_request.web_url,
        merge_requests=[],
        publish_errors=[],
        usage=None,
        question=None,
    )


async def _astop_job(
    context: TaskContext, client: RepoClient, merge_request: MergeRequest, thread_id: str
) -> AgentResult | None:
    """End the job's run FAILED, never started, when its merge request was merged or closed (saying so on it) or its
    session was archived; ``None`` when the job may start."""
    note = NOT_STARTED_MERGED if merge_request.merged else NOT_STARTED_CLOSED if merge_request.closed else None
    if note is None and not await Session.objects.filter(pk=thread_id).archived().aexists():
        return None
    task_run = await aget_task_run(context.task_result.id)
    if task_run.run_id is not None:
        run = await Run.objects.aget(pk=task_run.run_id)
        now = timezone.now()
        error = note or NOT_RUN_ARCHIVED
        await Run.objects.afinish(run, RunStatus.FAILED, never_started=True, error_message=error, finished_at=now)
    if note is None:
        logger.info(
            "Not starting the job on %s!%s: its session is archived",
            merge_request.repo_id,
            merge_request.merge_request_id,
        )
        return _skip_result("", merge_request)
    logger.info("Not starting the job on %s!%s: %s", merge_request.repo_id, merge_request.merge_request_id, note)
    client.create_merge_request_comment(merge_request.repo_id, merge_request.merge_request_id, body=note)
    return _skip_result(note, merge_request)


@task(dedup=True, dedup_on=("repo_id", "merge_request_id", "request_key"), takes_context=True)  # ty: ignore[no-matching-overload]
async def address_merge_request_task(
    context: TaskContext,
    repo_id: str,
    merge_request_id: int,
    thread_id: str,
    use_max: bool,
    request_key: str,
    mention_comment_id: str | None = None,
    mention_note_id: int | None = None,
    sandbox_environment_id: str | None = None,
    prompt: str | None = None,
    requester_username: str = "",
    agent_model: str | None = None,
    agent_thinking_level: str | None = None,
    references: list[dict[str, Any]] | None = None,
    mcp_overrides: dict[str, str] | None = None,
) -> AgentResult:
    """
    Run the agent on a merge request, for a comment that mentioned DAIV on it or a job's ``prompt``.

    A job does not start once its merge request was merged or closed, which it says on the merge request, or once its
    session was archived, which it doesn't; its run then ends FAILED. A mention on a merged merge request is answered
    that it was merged.

    Args:
        context (TaskContext): Names the task result the run's ``Run`` row is linked to.
        repo_id (str): The repository id.
        merge_request_id (int): The merge request id.
        thread_id (str): The LangGraph checkpoint key minted by the caller.
        use_max (bool): The max mode the caller decided and stamped on the ``Run``.
        request_key (str): What asked for the run, ``note:{mention_note_id}`` for a mention; with ``repo_id`` and the
            iid, the run's dedup key, so each reply in one GitLab discussion gets its own run.
        mention_comment_id (str | None): The discussion of the mention.
        mention_note_id (int | None): The note that mentioned DAIV, which the run answers.
        sandbox_environment_id (str | None): The env the caller selected; ``None``: the GLOBAL default.
        prompt (str | None): A job's prompt, in place of a mention.
        requester_username (str): Who sent the job, as the model sees them.
        agent_model (str | None): The job's model; ``None``: the default, or the max model with ``use_max``.
        agent_thinking_level (str | None): The thinking level for ``agent_model``.
        references (list[dict] | None): A job's stored external references, for this run only.
        mcp_overrides (dict[str, str] | None): A job's resolved MCP overrides, for this run only.
    """
    from sessions.merge_request_runs.addressor import CommentsAddressorManager

    client = RepoClient.create_instance()
    merge_request = client.get_merge_request(repo_id, merge_request_id)

    if prompt is not None and (stopped := await _astop_job(context, client, merge_request, thread_id)) is not None:
        return stopped
    if merge_request.merged:
        response = (
            f"This merge request has already been merged, so I can't act on comments left against "
            f"its branch (`{merge_request.source_branch}`). Please open a new issue or merge request "
            f"for any follow-up changes."
        )
        logger.warning("Skipping MR-comment run for %s!%s: already merged.", repo_id, merge_request_id)
        client.create_merge_request_comment(repo_id, merge_request_id, body=response)
        return _skip_result(response, merge_request)

    task_run = await aget_task_run(context.task_result.id)
    job: dict[str, Any] = {}
    if prompt is not None:
        job = {
            "prompt": prompt,
            "requester_username": requester_username,
            "agent_model": agent_model,
            "agent_thinking_level": agent_thinking_level,
            "references": references or [],
            "mcp_overrides": mcp_overrides or {},
        }
    try:
        return await CommentsAddressorManager.address_comments(
            repo_id=repo_id,
            merge_request=merge_request,
            mention_comment_id=mention_comment_id,
            mention_note_id=mention_note_id,
            thread_id=thread_id,
            sandbox_env_id=sandbox_environment_id,
            run_id=task_run.run_id,
            use_max=use_max,
            acting_user_id=task_run.user_id,
            **job,
        )
    except CloneRefNotFoundError:
        response = (
            f"The source branch `{merge_request.source_branch}` for this merge request no longer "
            f"exists, so I can't check it out to address this comment. If it was merged and deleted, "
            f"please open a new issue or merge request for any follow-up changes."
        )
        logger.warning(
            "Skipping MR-comment run for %s!%s: source branch %r no longer exists.",
            repo_id,
            merge_request_id,
            merge_request.source_branch,
        )
        client.create_merge_request_comment(repo_id, merge_request_id, body=response)
        return _skip_result(response, merge_request)
