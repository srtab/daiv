from __future__ import annotations

from django.utils import timezone

from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langgraph.graph.state import CompiledGraph
from langgraph.prebuilt import create_react_agent
from langgraph.prebuilt.chat_agent_executor import AgentState

from automation.agents import BaseAgent
from automation.conf import settings
from automation.tools.repository import SEARCH_CODE_SNIPPETS_NAME, SearchCodeSnippetsTool
from codebase.clients import RepoClient
from codebase.indexes import CodebaseIndex

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

        react_agent = create_react_agent(
            self.get_model(model=settings.CODEBASE_CHAT.MODEL_NAME, temperature=settings.CODEBASE_CHAT.TEMPERATURE),
            state_schema=CodebaseChatAgentState,
            tools=[SearchCodeSnippetsTool(api_wrapper=index)],
            prompt=ChatPromptTemplate.from_messages([codebase_chat_system, MessagesPlaceholder("messages")]).partial(
                repositories=index._get_all_repositories(),
                search_code_snippets_name=SEARCH_CODE_SNIPPETS_NAME,
                current_date_time=timezone.now().strftime("%d %B, %Y %H:%M"),
            ),
            version="v2",
            name="codebase_answer_react_agent",
        )
        return react_agent
