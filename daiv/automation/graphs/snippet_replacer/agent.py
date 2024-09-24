from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable
from langgraph.graph.state import CompiledStateGraph

from automation.graphs.agents import BaseAgent

from .prompts import human, system
from .schemas import SnippetReplacerOutput


class SnippetReplacerAgent(BaseAgent):
    """
    Agent to replace a code snippet in a codebase.
    """

    def compile(self) -> CompiledStateGraph | Runnable:
        prompt = ChatPromptTemplate.from_messages([SystemMessage(system), HumanMessage(human)])
        return prompt | self.model.with_structured_output(SnippetReplacerOutput)
