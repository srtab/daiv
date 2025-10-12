from __future__ import annotations

from django.utils import timezone

from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.config import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent
from langgraph.store.memory import InMemoryStore

from automation.agents import BaseAgent, ThinkingLevel
from automation.agents.tools.toolkits import FileNavigationToolkit
from codebase.context import get_repository_ctx

from .conf import settings
from .prompts import codebase_chat_system


class CodebaseChatAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent for answering questions about specific repository.

    Use `set_repository_ctx` to set the repository context before using this agent.
    """

    async def compile(self) -> CompiledStateGraph:
        """
        Compile the graph for the agent.

        Returns:
            CompiledStateGraph: The compiled graph.
        """
        return create_react_agent(
            BaseAgent.get_model(
                model=settings.MODEL_NAME, temperature=settings.TEMPERATURE, thinking_level=ThinkingLevel.LOW
            ),
            tools=FileNavigationToolkit.get_tools(),
            store=InMemoryStore(),
            prompt=ChatPromptTemplate.from_messages([codebase_chat_system, MessagesPlaceholder("messages")]).partial(
                current_date_time=timezone.now().strftime("%d %B, %Y"), repository=get_repository_ctx().repo_id
            ),
            name=settings.NAME,
        ).with_config(RunnableConfig(recursion_limit=settings.RECURSION_LIMIT))
