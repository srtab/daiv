from __future__ import annotations

from django.utils import timezone

from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest, dynamic_prompt
from langchain_anthropic.middleware.prompt_caching import AnthropicPromptCachingMiddleware
from langgraph.config import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.memory import InMemoryStore

from automation.agents import BaseAgent, ThinkingLevel
from automation.agents.middleware import InjectImagesMiddleware
from automation.agents.tools.toolkits import FileNavigationToolkit
from codebase.context import RuntimeCtx

from .conf import settings
from .prompts import codebase_chat_system


@dynamic_prompt
async def codebase_chat_system_prompt(request: ModelRequest) -> str:
    """
    Dynamic prompt for the codebase chat agent.

    Args:
        ctx: The runtime context.
    """
    return codebase_chat_system.format(
        current_date_time=timezone.now().strftime("%d %B, %Y"), repository=request.runtime.context.repo_id
    )


class CodebaseChatAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent for answering questions about specific repository.

    Use `set_runtime_ctx` to set the runtime context before using this agent.
    """

    async def compile(self) -> CompiledStateGraph:
        """
        Compile the graph for the agent.

        Returns:
            CompiledStateGraph: The compiled graph.
        """
        model = BaseAgent.get_model(
            model=settings.MODEL_NAME, temperature=settings.TEMPERATURE, thinking_level=ThinkingLevel.LOW
        )
        return create_agent(
            model=model,
            tools=FileNavigationToolkit.get_tools(),
            store=InMemoryStore(),
            context_schema=RuntimeCtx,
            middleware=[
                codebase_chat_system_prompt,
                InjectImagesMiddleware(image_inputs_supported=model.profile.get("image_inputs", True)),
                AnthropicPromptCachingMiddleware(),
            ],
            name=settings.NAME,
        ).with_config(RunnableConfig(recursion_limit=settings.RECURSION_LIMIT))
