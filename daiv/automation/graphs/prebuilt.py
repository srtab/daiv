from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from langchain_core.messages import AIMessage, BaseMessage
from langgraph.graph import END, StateGraph
from langgraph.prebuilt.chat_agent_executor import AgentState
from langgraph.prebuilt.tool_node import ToolNode
from pydantic import BaseModel  # noqa: TCH002

from automation.graphs.agents import BaseAgent

if TYPE_CHECKING:
    from collections.abc import Hashable

    from langchain_core.language_models import LanguageModelInput
    from langchain_core.runnables import Runnable, RunnableConfig
    from langchain_core.tools.base import BaseTool
    from langchain_openai import ChatOpenAI
    from langgraph.graph.state import CompiledStateGraph


class StructuredAgentState(AgentState):
    response: BaseModel | None


class REACTAgent(BaseAgent):
    """
    Agent to interact with a model and tools in a reactive way.

    Based on ReAct agent pattern (https://arxiv.org/abs/2210.03629).

    For more information, see:
    https://langchain-ai.github.io/langgraph/how-tos/react-agent-structured-output/
    https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/prebuilt/chat_agent_executor.py#L158
    """

    model_name = "gpt-4o-2024-08-06"

    def __init__(self, tools: list[BaseTool], *args, with_structured_output: type[BaseModel] | None = None, **kwargs):
        self.tool_classes = tools
        self.with_structured_output = with_structured_output
        self.structured_tool_name = None
        self.state_class = AgentState
        if self.with_structured_output:
            self.tool_classes.append(self.with_structured_output)
            self.structured_tool_name = self.with_structured_output.model_json_schema()["title"]
            self.state_class = StructuredAgentState
        super().__init__(*args, **kwargs)

    def get_model(self) -> ChatOpenAI | Runnable[LanguageModelInput, BaseMessage]:
        """
        Rewrite the get_model method to bind the tools to the model.
        """
        tools_kwargs = {}
        if self.with_structured_output:
            # Use strict mode and any to increase chances of model calling the structured tool.
            tools_kwargs = {"tool_choice": "any", "parallel_tool_calls": False, "strict": True}
        return super().get_model().bind_tools(self.tool_classes, **tools_kwargs)

    def compile(self) -> CompiledStateGraph | Runnable:
        """
        Compile the workflow for the agent.
        """
        workflow = StateGraph(self.state_class)

        workflow.add_node("agent", self.call_model)
        workflow.add_node("tools", ToolNode(self.tool_classes))
        if self.with_structured_output:
            workflow.add_node("respond", self.respond)

        workflow.set_entry_point("agent")

        path_map: dict[Hashable, str] = {"continue": "tools"}
        if self.with_structured_output:
            path_map["respond"] = "respond"
        else:
            path_map["end"] = END

        workflow.add_conditional_edges("agent", self.should_continue, path_map)

        workflow.add_edge("tools", "agent")
        if self.with_structured_output:
            workflow.add_edge("respond", END)

        return workflow.compile()

    def call_model(self, state: AgentState, config: RunnableConfig):
        """
        Call the model with the current state and configuration.

        Args:
            state (AgentState): The current state of the agent.
            config (RunnableConfig): The configuration for the agent.

        Returns:
            dict: The response from the model.
        """
        response = self.model.invoke(state["messages"], config)
        if state["is_last_step"] and isinstance(response, AIMessage) and response.tool_calls:
            return {"messages": [AIMessage(id=response.id, content="Sorry, need more steps to process this request.")]}
        return {"messages": [response]}

    def respond(self, state: AgentState):
        """
        Respond to the user with the final response.

        Args:
            state (AgentState): The current state of the agent.

        Returns:
            dict: The final response from the agent.
        """
        if not self.with_structured_output:
            raise ValueError("No structured output model provided.")

        last_message = state["messages"][-1]
        return {"response": self.with_structured_output(**last_message.tool_calls[0]["args"])}

    def should_continue(self, state: AgentState) -> Literal["respond", "continue", "end"]:
        """
        Check if the agent should continue or end.

        Args:
            state (AgentState): The current state of the agent.

        Returns:
            str: The next step for the agent.
        """
        last_message = state["messages"][-1]

        if (
            last_message.tool_calls
            and self.structured_tool_name
            and last_message.tool_calls[0]["name"] == self.structured_tool_name
        ):
            return "respond"
        elif not last_message.tool_calls:
            return "end"
        return "continue"
