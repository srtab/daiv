from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnablePassthrough
from langgraph.graph.state import CompiledStateGraph

from automation.graphs.agents import BaseAgent

from .prompts import human, system
from .schemas import PullRequestDescriberOutput


class PullRequestDescriberAgent(BaseAgent):
    """
    Agent to describe changes in a pull request.
    """

    def compile(self) -> CompiledStateGraph | Runnable:
        prompt = ChatPromptTemplate.from_messages([SystemMessage(system), HumanMessage(human)])
        return (
            {"changes": RunnablePassthrough()} | prompt | self.model.with_structured_output(PullRequestDescriberOutput)
        )
