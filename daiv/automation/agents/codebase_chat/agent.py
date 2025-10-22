from __future__ import annotations

from django.utils import timezone

from langchain.agents import create_agent
from langchain_anthropic.middleware.prompt_caching import AnthropicPromptCachingMiddleware
from langgraph.config import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.store.memory import InMemoryStore

from automation.agents import BaseAgent, ThinkingLevel
from automation.agents.middleware import InjectImagesMiddleware
from automation.agents.tools.toolkits import FileNavigationToolkit
from codebase.context import get_runtime_ctx

from .conf import settings
from .prompts import codebase_chat_system


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
        system_prompt = codebase_chat_system.format(
            current_date_time=timezone.now().strftime("%d %B, %Y"), repository=get_runtime_ctx().repo_id
        )
        return create_agent(
            BaseAgent.get_model(
                model=settings.MODEL_NAME, temperature=settings.TEMPERATURE, thinking_level=ThinkingLevel.LOW
            ),
            tools=FileNavigationToolkit.get_tools(),
            store=InMemoryStore(),
            system_prompt=system_prompt,
            middleware=[InjectImagesMiddleware(), AnthropicPromptCachingMiddleware()],
            name=settings.NAME,
        ).with_config(RunnableConfig(recursion_limit=settings.RECURSION_LIMIT))
