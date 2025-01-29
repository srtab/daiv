import logging
from typing import Any

from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import Runnable, RunnableLambda
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field, HttpUrl

from automation.agents import BaseAgent
from automation.conf import settings
from automation.tools.repository import SearchCodeSnippetsTool
from codebase.indexes import CodebaseIndex

from .prompts import data_collection_system

logger = logging.getLogger("daiv.agents")


class FinalAnswer(BaseModel):
    """
    The final answer to the user's query based on the collected data with references to the codebase files
    or repositories.
    """

    content: str = Field(
        description=(
            "A clear, accurate, and contextually grounded answer based solely on the collected data and the user query."
            "It should be helpful and use a tech enthusiast tone. Include a pro tip whenever possible."
        )
    )
    references: list[HttpUrl] = Field(
        description=(
            "Full links to the files or repositories relevant to the user query. "
            "Don't try to guess the links, use the `external_link` from the <CodeSnippet> tags."
        ),
        default_factory=list,
    )


class CodebaseQAAgent(BaseAgent[Runnable[dict[str, Any], FinalAnswer]]):
    """
    Agent to answer questions about the codebase.
    """

    model_name = settings.CODING_COST_EFFICIENT_MODEL_NAME

    def __init__(self, index: CodebaseIndex):
        self.index = index
        super().__init__()

    def compile(self) -> Runnable:
        return RunnableLambda(self._execute_react_agent) | self.model.with_structured_output(FinalAnswer)

    def _execute_react_agent(self, inputs):
        react_agent = create_react_agent(
            self.model,
            tools=[SearchCodeSnippetsTool(api_wrapper=self.index)],
            state_modifier=ChatPromptTemplate.from_messages([data_collection_system, MessagesPlaceholder("messages")]),
        )
        result = react_agent.invoke(inputs)

        return result["messages"][:-1]
