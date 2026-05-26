import logging

from django_tasks import task
from langchain_core.messages import HumanMessage

from automation.agent.graph import create_daiv_agent
from automation.agent.results import AgentResult, build_agent_result
from automation.agent.usage_tracking import build_usage_summary, track_usage_metadata
from automation.agent.utils import build_langsmith_config, extract_text_content, get_daiv_agent_kwargs
from codebase.base import Scope
from codebase.context import set_runtime_ctx
from core.checkpointer import open_checkpointer

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
) -> AgentResult:
    """Run the DAIV agent for a submitted job and return a standardized result.

    The ``thread_id`` is used as the LangGraph checkpoint key. Callers MUST mint one
    up-front and persist it on the corresponding ``Activity`` — chat resume is built
    on the assumption that the activity row and the checkpointer share the same key.
    A silent UUID fallback here would break that contract on the resume path.

    ``sandbox_environment_id``, when provided, is forwarded to ``set_runtime_ctx``.
    Webhook callers (issue/review addressors) bypass this task and call
    ``create_daiv_agent`` directly; ``use_max`` is therefore not accepted here.
    """
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

    input_data = {"messages": [HumanMessage(content=prompt)]}

    try:
        async with (
            set_runtime_ctx(
                repo_id=repo_id, scope=Scope.GLOBAL, ref=ref, sandbox_env_id=sandbox_environment_id
            ) as runtime_ctx,
            open_checkpointer() as checkpointer,
        ):
            agent_kwargs = get_daiv_agent_kwargs(
                model_config=runtime_ctx.config.models.agent,
                agent_model=agent_model,
                agent_thinking_level=agent_thinking_level,
            )
            config = build_langsmith_config(
                runtime_ctx,
                trigger="job",
                model=agent_kwargs["model_names"][0],
                thinking_level=agent_kwargs["thinking_level"],
                extra_metadata={"ref": ref, "override_source": "explicit" if agent_model else None},
                configurable={"thread_id": thread_id},
            )
            daiv_agent = await create_daiv_agent(ctx=runtime_ctx, checkpointer=checkpointer, **agent_kwargs)
            with track_usage_metadata() as usage_handler:
                result = await daiv_agent.ainvoke(input_data, config=config, context=runtime_ctx)
    except Exception:
        logger.exception("Job failed for repo_id=%s, ref=%s, agent_model=%s", repo_id, ref, agent_model or "<auto>")
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
