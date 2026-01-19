from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from django.utils import timezone

from deepagents.backends import FilesystemBackend
from deepagents.graph import create_agent
from deepagents.middleware.memory import MemoryMiddleware
from langchain.agents.middleware import dynamic_prompt
from langchain_core.prompts import ChatPromptTemplate

from automation.agent import BaseAgent
from automation.agent.constants import PROJECT_MEMORY_PATH
from automation.agent.middlewares.prompt_cache import AnthropicPromptCachingMiddleware
from codebase.context import RuntimeCtx

from .prompts import human, system
from .schemas import PullRequestMetadata

if TYPE_CHECKING:
    from langchain.agents.middleware.types import ModelRequest
    from langchain_core.runnables import Runnable

    from automation.agent.constants import ModelName


@dynamic_prompt
def dynamic_pr_describer_system_prompt(request: ModelRequest) -> str:
    """
    Dynamic system prompt for the PR describer agent.
    """
    return (
        request.system_prompt + "\n\n" + system.format(current_date_time=timezone.now().strftime("%d %B, %Y")).content
    )


def create_pr_describer_agent(model: ModelName | str, *, ctx: RuntimeCtx) -> Runnable:
    """
    Create the PR describer agent.

    Args:
        model: The model to use for the agent.
        ctx: The runtime context.

    Returns:
        The PR describer agent.
    """
    agent_path = Path(ctx.repo.working_dir)
    backend = FilesystemBackend(root_dir=agent_path.parent, virtual_mode=True)

    return ChatPromptTemplate.from_messages([human]).partial(extra_context="") | create_agent(
        model=BaseAgent.get_model(model=model),
        tools=[],  # No tools are needed for this agent, it only uses the memory and the system prompt
        middleware=[
            MemoryMiddleware(
                backend=backend,
                sources=[
                    f"/{agent_path.name}/{ctx.config.context_file_name}",
                    f"/{agent_path.name}/{PROJECT_MEMORY_PATH}",
                ],
            ),
            AnthropicPromptCachingMiddleware(),
            dynamic_pr_describer_system_prompt,
        ],
        response_format=PullRequestMetadata,
        context_schema=RuntimeCtx,
        name="PR Describer Agent",
    )
