from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.tasks import task

from asgiref.sync import sync_to_async

from codebase.base import DiffLocation
from codebase.clients import RepoClient
from codebase.clients.base import CommentThread
from codebase.exceptions import CloneRefNotFoundError
from sessions.inbox.drain import Drain
from sessions.merge_request_runs.guards import JobGuard, JobNotStarted, MentionGuard
from sessions.services import aget_task_run

if TYPE_CHECKING:
    from django.tasks import TaskContext

    from sessions.executor.result import AgentResult


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
    mention_location: dict[str, Any] | None = None,
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

    What a run that can't go ahead does is the guards' (:mod:`sessions.merge_request_runs.guards`): a job checks its
    merge request and session again once its run holds the session's slot.

    Args:
        context (TaskContext): Names the task result the run's ``Run`` row is linked to.
        repo_id (str): The repository id.
        merge_request_id (int): The merge request id.
        thread_id (str): The LangGraph checkpoint key minted by the caller.
        use_max (bool): The max mode the caller decided and stamped on the ``Run``.
        request_key (str): What asked for the run (``SourceKey.note``, ``SourceKey.review_comment`` or
            ``SourceKey.job``); part of its dedup key.
        mention_comment_id (str | None): The discussion of the mention.
        mention_note_id (int | None): The note that mentioned DAIV, which the run answers.
        mention_location (dict | None): A dumped ``DiffLocation``: the note is on the diff, so the run reads it there,
            tells the agent where, and answers in its thread.
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
    if prompt is None:
        thread = CommentThread(mention_comment_id, on_diff=True) if mention_location and mention_comment_id else None
        guard = MentionGuard(client, merge_request, reply_to=thread)
        recheck = None
    else:
        guard = JobGuard(client, merge_request, thread_id=thread_id, task_result_id=context.task_result.id)
        recheck = guard.arecheck
    if (skipped := await guard.askip()) is not None:
        return skipped

    task_run = await aget_task_run(context.task_result.id)
    try:
        return await CommentsAddressorManager.address_comments(
            repo_id=repo_id,
            merge_request=merge_request,
            mention_comment_id=mention_comment_id,
            mention_note_id=mention_note_id,
            mention_location=DiffLocation.model_validate(mention_location) if mention_location else None,
            thread_id=thread_id,
            sandbox_env_id=sandbox_environment_id,
            run_id=task_run.run_id,
            use_max=use_max,
            acting_user_id=task_run.user_id,
            prompt=prompt,
            requester_username=requester_username,
            agent_model=agent_model,
            agent_thinking_level=agent_thinking_level,
            references=references,
            mcp_overrides=mcp_overrides,
            on_context_ready=recheck,
        )
    except JobNotStarted as stopped:
        # Its run ended while the slot was held, so that end's drain found the session busy.
        await sync_to_async(Drain(thread_id).start_logged)()
        return stopped.result
    except CloneRefNotFoundError:
        return await guard.abranch_gone()
