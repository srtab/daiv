import logging

from django_tasks import task
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from automation.agent.graph import create_daiv_agent
from automation.agent.utils import extract_text_content, get_daiv_agent_kwargs
from codebase.base import Scope
from codebase.context import set_runtime_ctx

logger = logging.getLogger("daiv.jobs")


@task()
async def run_job_task(repo_id: str, prompt: str, ref: str | None = None, use_max: bool = False) -> str:
    """
    Run the DAIV agent for a submitted job and return the text output.

    Args:
        repo_id: The repository id.
        prompt: The user prompt to send to the agent.
        ref: The git reference. Defaults to the repository's default branch.
        use_max: Whether to use the max model configuration.
    """
    logger.info("Starting job for repo_id=%s, ref=%s, use_max=%s", repo_id, ref, use_max)

    input_data = {"messages": [HumanMessage(content=prompt)]}

    try:
        async with set_runtime_ctx(repo_id=repo_id, scope=Scope.GLOBAL, ref=ref) as runtime_ctx:
            agent_kwargs = get_daiv_agent_kwargs(model_config=runtime_ctx.config.models.agent, use_max=use_max)
            config = RunnableConfig(
                metadata={
                    "repo_id": repo_id,
                    "ref": ref,
                    "trigger": "job",
                    "model": agent_kwargs["model_names"][0],
                    "thinking_level": agent_kwargs["thinking_level"],
                }
            )
            daiv_agent = await create_daiv_agent(ctx=runtime_ctx, **agent_kwargs)
            result = await daiv_agent.ainvoke(input_data, config=config, context=runtime_ctx)
    except Exception:
        logger.exception("Job failed for repo_id=%s, ref=%s, use_max=%s", repo_id, ref, use_max)
        raise

    messages = result.get("messages")
    if not messages:
        logger.error("Job for repo_id=%s produced no messages", repo_id)
        raise ValueError(f"Agent returned no messages for repo_id={repo_id}")

    logger.info("Job completed for repo_id=%s", repo_id)
    return extract_text_content(messages[-1].content)
