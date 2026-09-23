import logging
from typing import TYPE_CHECKING

from django_tasks import task
from sessions.executor.lock import LOCK_WAIT_TIMEOUT_S, NoLock, Wait
from sessions.executor.run import execute_run
from sessions.executor.spec import RunHooks, RunSpec
from sessions.models import Session

from codebase.base import Scope

if TYPE_CHECKING:
    from automation.agent.results import AgentResult

logger = logging.getLogger("daiv.jobs")


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
) -> AgentResult:
    """Run the DAIV agent for a submitted job and return a standardized result.

    The ``thread_id`` is used as the LangGraph checkpoint key. Callers MUST mint one
    up-front and persist it on the corresponding ``Run`` — chat resume is built
    on the assumption that the Session and the checkpointer share the same thread_id key.
    A silent UUID fallback here would break that contract on the resume path.

    ``sandbox_environment_id``, when provided, is forwarded to ``set_runtime_ctx``.
    ``user_id``: DAIV user id that triggered the run; forwarded as ``acting_user_id``
    to select the user's personal MCP servers.
    Webhook callers (issue/review addressors) bypass this task; ``use_max`` is therefore
    not accepted here.
    """
    # Heavy imports live here so enqueue-side importers of this module stay light.
    from langchain_core.messages import HumanMessage

    if not thread_id:
        raise ValueError("run_job_task requires a non-empty thread_id; mint one before enqueueing")

    logger.info(
        "Starting job for repo_id=%s, ref=%s, agent_model=%s, agent_thinking_level=%s, thread_id=%s, sandbox_env_id=%s",
        repo_id,
        ref,
        agent_model or "<auto>",
        agent_thinking_level or "<auto>",
        thread_id,
        sandbox_environment_id,
    )

    session_row = (
        await Session.objects.filter(pk=thread_id).only("thread_id", "mcp_overrides", "external_refs").afirst()
    )
    if session_row is None:
        logger.warning("run_job_task: no session row for thread_id=%s; running without lock", thread_id)
        lock, mcp_overrides, references = NoLock(), {}, ()
    else:
        lock = Wait(holder_id=run_id or f"job-{thread_id[:8]}", timeout_s=LOCK_WAIT_TIMEOUT_S)
        mcp_overrides, references = session_row.mcp_overrides, session_row.external_references()

    async def _log_failure(exc: Exception, *, draft_published: bool) -> None:
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
            lock=lock,
            ref=ref,
            agent_model=agent_model,
            agent_thinking_level=agent_thinking_level,
            sandbox_env_id=sandbox_environment_id,
            acting_user_id=user_id,
            mcp_overrides=mcp_overrides,
            references=references,
            run_id=run_id,
            persist_ref=True,
            arm_watch=True,
            extra_metadata={"ref": ref, "override_source": "explicit" if agent_model else None},
        ),
        RunHooks(on_failure=_log_failure),
    )

    logger.info("Job completed for repo_id=%s, thread_id=%s", repo_id, thread_id)
    return outcome.agent_result
