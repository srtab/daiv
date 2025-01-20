import logging

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from automation.agents import BaseAgent
from automation.conf import settings
from automation.tools.repository import SearchCodeSnippetsTool
from automation.tools.web_search import WebSearchTool
from codebase.indexes import CodebaseIndex

from .prompts import system
from .state import OverallState

logger = logging.getLogger("daiv.agents")


class CodebaseQAAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent to answer questions about the codebase.
    """

    model_name = settings.CODING_COST_EFFICIENT_MODEL_NAME

    def __init__(self, index: CodebaseIndex):
        self.index = index
        super().__init__()

    def get_model_kwargs(self) -> dict:
        kwargs = super().get_model_kwargs()
        kwargs["temperature"] = 0.3
        return kwargs

    def compile(self) -> CompiledStateGraph:
        workflow = StateGraph(OverallState)

        # Add nodes
        workflow.add_node("retrieve", self.retrieve)
        workflow.add_node("generate", self.generate)

        # Add edges
        workflow.add_edge(START, "retrieve")
        workflow.add_edge("retrieve", "generate")
        workflow.add_edge("generate", END)

        return workflow.compile()

    def retrieve(self, state: OverallState):
        """
        Retrieve the context.
        """
        context = SearchCodeSnippetsTool(api_wrapper=self.index).invoke({
            "query": state["messages"][-1].content,
            "intent": "Searching the codebase.",
        })
        if not context:
            context = WebSearchTool().invoke({
                "query": state["messages"][-1].content,
                "intent": "No code snippets found, searching the web.",
            })
        return {"context": context}

    def generate(self, state: OverallState):
        """
        Generate answer.
        """
        prompt = [
            system.format(
                context=state["context"],
                codebase_client=self.index.repo_client.client_slug,
                codebase_url=self.index.repo_client.codebase_url,
            )
        ] + state["messages"]

        return {"messages": [self.model.invoke(prompt)]}
