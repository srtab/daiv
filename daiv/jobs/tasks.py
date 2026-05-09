import logging

from activity.models import Activity
from django_tasks import task
from langchain_core.messages import HumanMessage

from automation.agent.graph import create_daiv_agent
from automation.agent.results import AgentResult, build_agent_result
from automation.agent.usage_tracking import build_usage_summary, track_usage_metadata
from automation.agent.utils import build_langsmith_config, extract_text_content, get_daiv_agent_kwargs
from codebase.base import Scope
from codebase.context import set_runtime_ctx
from codebase.exceptions import InvalidThreadResumeError
from core.checkpointer import open_checkpointer

logger = logging.getLogger("daiv.jobs")


async def _check_resume_consistency(thread_id: str, repo_id: str | None) -> None:
    """Reject mismatched repo/repoless resume of an existing thread.

    A thread first bound to a repository cannot be resumed in repoless mode (and
    vice versa): the agent state carried by the checkpointer assumes one mode or
    the other, and silently switching would corrupt the conversation.
    """
    prior = await Activity.objects.filter(thread_id=thread_id).order_by("created_at").values("repo_id").afirst()
    if prior is None:
        return  # first activity for this thread; nothing to compare against

    prior_repo_id = prior["repo_id"]
    if (prior_repo_id is None) != (repo_id is None):
        raise InvalidThreadResumeError(thread_id=thread_id, expected=prior_repo_id, got=repo_id)


@task()
async def run_job_task(
    repo_id: str | None, prompt: str, thread_id: str, ref: str | None = None, use_max: bool = False
) -> AgentResult:
    """Run the DAIV agent for a submitted job and return a standardized result.

    The ``thread_id`` is used as the LangGraph checkpoint key. Callers MUST mint one
    up-front and persist it on the corresponding ``Activity`` — chat resume is built
    on the assumption that the activity row and the checkpointer share the same key.
    """
    if not thread_id:
        raise ValueError("run_job_task requires a non-empty thread_id; mint one before enqueueing")

    await _check_resume_consistency(thread_id, repo_id)

    logger.info("Starting job for repo_id=%s, ref=%s, use_max=%s, thread_id=%s", repo_id, ref, use_max, thread_id)

    input_data = {"messages": [HumanMessage(content=prompt)]}

    try:
        async with (
            set_runtime_ctx(repo_id=repo_id, scope=Scope.GLOBAL, ref=ref) as runtime_ctx,
            open_checkpointer() as checkpointer,
        ):
            agent_kwargs = get_daiv_agent_kwargs(model_config=runtime_ctx.config.models.agent, use_max=use_max)
            config = build_langsmith_config(
                runtime_ctx,
                trigger="job",
                model=agent_kwargs["model_names"][0],
                thinking_level=agent_kwargs["thinking_level"],
                extra_metadata={"ref": ref},
                configurable={"thread_id": thread_id},
            )
            daiv_agent = await create_daiv_agent(
                ctx=runtime_ctx, thread_id=thread_id, checkpointer=checkpointer, **agent_kwargs
            )
            with track_usage_metadata() as usage_handler:
                result = await daiv_agent.ainvoke(input_data, config=config, context=runtime_ctx)
    except Exception:
        logger.exception("Job failed for repo_id=%s, ref=%s, use_max=%s", repo_id, ref, use_max)
        raise

    messages = result.get("messages")
    if not messages:
        logger.error("Job for repo_id=%s produced no messages", repo_id)
        raise ValueError(f"Agent returned no messages for repo_id={repo_id}")

    response_text = extract_text_content(messages[-1].content)

    logger.info("Job completed for repo_id=%s, thread_id=%s", repo_id, thread_id)
    return await build_agent_result(
        daiv_agent, config, response=response_text, usage=build_usage_summary(usage_handler).to_dict()
    )
