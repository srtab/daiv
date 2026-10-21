import logging
from typing import TYPE_CHECKING

from django_tasks import task

from codebase.base import Scope
from sessions.executor.lock import wait_for_slot
from sessions.executor.spec import RunHooks, RunSpec
from sessions.models import Session

if TYPE_CHECKING:
    from langgraph.types import StateSnapshot

    from sessions.executor.result import AgentResult

logger = logging.getLogger("daiv.sessions")


@task()
async def run_job_task(
    repo_id: str,
    prompt: str,
    thread_id: str,
    ref: str | None = None,
    agent_model: str | None = None,
    agent_thinking_level: str | None = None,
    sandbox_environment_id: str | None = None,
    run_id: str | None = None,
    user_id: int | None = None,
    ask_user_enabled: bool = True,
    fork_branch: bool = False,
) -> AgentResult:
    """Run the DAIV agent for a submitted job and return a standardized result.

    The ``thread_id`` is used as the LangGraph checkpoint key. Callers MUST mint one
    up-front and persist it on the corresponding ``Run`` — chat resume is built
    on the assumption that the Session and the checkpointer share the same thread_id key.
    A silent UUID fallback here would break that contract on the resume path.

    ``sandbox_environment_id`` is the environment the caller selected; ``None`` runs on the GLOBAL default.
    ``user_id``: DAIV user id that triggered the run; forwarded as ``acting_user_id``
    to select the user's personal MCP servers.
    ``ask_user_enabled``: whether someone can answer a question the agent asks mid-run.
    ``fork_branch``: publish to a new branch and merge request even when ``ref`` has an open one (a delegated leg).
    Webhook callers (issue/review addressors) bypass this task; ``use_max`` is therefore
    not accepted here.
    """
    # Deferred: enqueue-side importers stay light, and executor.run -> sessions.services -> sessions.queue imports
    # this module.
    from langchain_core.messages import HumanMessage

    from sessions.executor.run import execute_run

    if not thread_id:
        raise ValueError("run_job_task requires a non-empty thread_id; mint one before enqueueing")

    logger.info(
        "Starting job for repo_id=%s, ref=%s, agent_model=%s, agent_thinking_level=%s, thread_id=%s",
        repo_id,
        ref,
        agent_model or "<auto>",
        agent_thinking_level or "<auto>",
        thread_id,
    )

    # Every enqueue goes through ``sessions.queue.aenqueue_run``, whose ``Run`` holds this row.
    session_row = await Session.objects.only("thread_id", "mcp_overrides", "external_refs").aget(pk=thread_id)

    async def _log_failure(exc: Exception, *, draft_published: bool, snapshot: StateSnapshot | None) -> None:
        logger.error(
            "Job failed for repo_id=%s, ref=%s, agent_model=%s", repo_id, ref, agent_model or "<auto>", exc_info=exc
        )

    outcome = await execute_run(
        RunSpec(
            thread_id=thread_id,
            repo_id=repo_id,
            scope=Scope.GLOBAL,
            input_messages=(HumanMessage(content=prompt),),
            trigger="job",
            lock=wait_for_slot(run_id or f"job-{thread_id[:8]}"),
            ref=ref,
            agent_model=agent_model,
            agent_thinking_level=agent_thinking_level,
            sandbox_env_id=sandbox_environment_id,
            acting_user_id=user_id,
            mcp_overrides=session_row.mcp_overrides,
            references=session_row.external_references(),
            run_id=run_id,
            persist_ref=True,
            arm_watch=True,
            ask_user_enabled=ask_user_enabled,
            agent_options={"fork_branch": fork_branch},
            extra_metadata={"ref": ref, "override_source": "explicit" if agent_model else None},
        ),
        RunHooks(on_failure=_log_failure),
    )

    logger.info("Job completed for repo_id=%s, thread_id=%s", repo_id, thread_id)
    return outcome.agent_result
