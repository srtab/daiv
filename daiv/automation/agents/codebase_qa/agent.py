import logging

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from automation.agents import BaseAgent
from automation.agents.base import CODING_COST_EFFICIENT_MODEL_NAME
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

    model_name = CODING_COST_EFFICIENT_MODEL_NAME

    def __init__(self, index: CodebaseIndex):
        self.index = index
        self.tools = [SearchCodeSnippetsTool(api_wrapper=index), WebSearchTool()]
        super().__init__()

    def compile(self) -> CompiledStateGraph:
        workflow = StateGraph(OverallState)

        # Add nodes
        workflow.add_node("query_or_respond", self.query_or_respond)
        workflow.add_node("tools", ToolNode(self.tools))
        workflow.add_node("generate", self.generate)

        # Add edges
        workflow.add_edge(START, "query_or_respond")
        workflow.add_conditional_edges("query_or_respond", tools_condition, {END: END, "tools": "tools"})
        workflow.add_edge("tools", "generate")
        workflow.add_edge("generate", END)

        return workflow.compile()

    def query_or_respond(self, state: OverallState):
        """
        Generate tool call for retrieval or respond.
        """
        llm_with_tools = self.model.bind_tools(self.tools)
        response = llm_with_tools.invoke(state["messages"])
        return {"messages": [response]}

    def generate(self, state: OverallState):
        """
        Generate answer.
        """
        recent_tool_messages = []
        for message in reversed(state["messages"]):
            if message.type == "tool":
                recent_tool_messages.append(message)
            else:
                break

        docs_content = "\n\n".join(doc.content for doc in recent_tool_messages[::-1])

        conversation_messages = [
            message
            for message in state["messages"]
            if message.type in ("human", "system") or (message.type == "ai" and not message.tool_calls)
        ]
        prompt = [system.format(context=docs_content)] + conversation_messages

        response = self.model.invoke(prompt)
        return {"messages": [response]}
