from __future__ import annotations

from django.utils import timezone

from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import create_react_agent
from langgraph.store.memory import InMemoryStore

from automation.agents import BaseAgent, ThinkingLevel
from automation.tools.repository import RepositoryStructureTool, RetrieveFileContentTool, SearchCodeSnippetsTool
from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex

from .conf import settings
from .prompts import codebase_chat_system


class CodebaseChatAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent for answering questions about codebases.
    """

    async def compile(self) -> CompiledStateGraph:
        """
        Compile the graph for the agent.

        Returns:
            CompiledStateGraph: The compiled graph.
        """
        repo_client = RepoClient.create_instance()
        index = CodebaseIndex(repo_client)

        return create_react_agent(
            BaseAgent.get_model(
                model=settings.MODEL_NAME, temperature=settings.TEMPERATURE, thinking_level=ThinkingLevel.LOW
            ),
            store=InMemoryStore(),
            tools=[
                SearchCodeSnippetsTool(api_wrapper=index, all_repositories=True),
                RepositoryStructureTool(api_wrapper=index, all_repositories=True),
                RetrieveFileContentTool(api_wrapper=repo_client, all_repositories=True),
            ],
            prompt=ChatPromptTemplate.from_messages([codebase_chat_system, MessagesPlaceholder("messages")]).partial(
                repositories=await index._get_all_repositories(), current_date_time=timezone.now().strftime("%d %B, %Y")
            ),
            version="v2",
            name=settings.NAME,
        )
