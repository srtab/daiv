from __future__ import annotations

from django.utils import timezone

from asgiref.sync import sync_to_async
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.graph.state import CompiledGraph
from langgraph.prebuilt import create_react_agent
from langgraph.prebuilt.chat_agent_executor import AgentState

from automation.agents import BaseAgent
from automation.tools.repository import SEARCH_CODE_SNIPPETS_NAME, SearchCodeSnippetsTool
from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex

from .conf import settings
from .prompts import codebase_chat_system


class CodebaseChatAgentState(AgentState):
    repositories: list[str]
    search_code_snippets_name: str
    current_date_time: str


class CodebaseChatAgent(BaseAgent[CompiledGraph]):
    """
    Agent for answering questions about codebases.
    """

    def compile(self) -> CompiledGraph:
        """
        Compile the graph for the agent.

        Returns:
            CompiledGraph: The compiled graph.
        """
        index = CodebaseIndex(RepoClient.create_instance())
        return create_react_agent(
            self.get_model(model=settings.MODEL_NAME, temperature=settings.TEMPERATURE),
            state_schema=CodebaseChatAgentState,
            tools=[SearchCodeSnippetsTool(api_wrapper=index, all_repositories=True)],
            prompt=ChatPromptTemplate.from_messages([codebase_chat_system, MessagesPlaceholder("messages")]).partial(
                repositories=index._get_all_repositories(),
                search_code_snippets_name=SEARCH_CODE_SNIPPETS_NAME,
                current_date_time=timezone.now().strftime("%d %B, %Y %H:%M"),
            ),
            version="v2",
            name=settings.NAME,
        )

    async def acompile(self) -> CompiledGraph:
        """
        Compile the graph for the agent asynchronously.

        Returns:
            CompiledGraph: The compiled graph.
        """
        index = CodebaseIndex(RepoClient.create_instance())
        return create_react_agent(
            self.get_model(model=settings.MODEL_NAME, temperature=settings.TEMPERATURE),
            state_schema=CodebaseChatAgentState,
            tools=[SearchCodeSnippetsTool(api_wrapper=index, all_repositories=True)],
            prompt=ChatPromptTemplate.from_messages([codebase_chat_system, MessagesPlaceholder("messages")]).partial(
                repositories=await sync_to_async(index._get_all_repositories)(),
                search_code_snippets_name=SEARCH_CODE_SNIPPETS_NAME,
                current_date_time=timezone.now().strftime("%d %B, %Y %H:%M"),
            ),
            version="v2",
            name=settings.NAME,
        )
