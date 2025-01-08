from __future__ import annotations

import logging
import textwrap
import uuid
from typing import TYPE_CHECKING, Literal, cast

from anthropic import InternalServerError as AnthropicInternalServerError
from langchain_core.messages import AIMessage, HumanMessage, ToolCall, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt.chat_agent_executor import AgentState
from langgraph.prebuilt.tool_node import ToolNode
from openai import InternalServerError as OpenAIInternalServerError
from pydantic import BaseModel, ValidationError  # noqa: TCH002

from automation.agents import BaseAgent
from automation.conf import settings
from automation.tools.repository import RETRIEVE_FILE_CONTENT_NAME, RetrieveFileContentTool

if TYPE_CHECKING:
    from collections.abc import Hashable, Sequence

    from langchain_core.prompts.chat import MessageLikeRepresentation
    from langchain_core.tools.base import BaseTool
    from langgraph.store.base import BaseStore

    from codebase.clients import AllRepoClient


logger = logging.getLogger("daiv.agents")


class StructuredAgentState(AgentState):
    response: BaseModel | None


class REACTAgent(BaseAgent[CompiledStateGraph]):
    """
    Agent to interact with a model and tools in a reactive way.

    Based on ReAct agent pattern (https://arxiv.org/abs/2210.03629).

    For more information, see:
    https://langchain-ai.github.io/langgraph/how-tos/react-agent-structured-output/
    https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/prebuilt/chat_agent_executor.py#L158
    """

    def __init__(
        self,
        tools: Sequence[BaseTool | type[BaseModel]],
        *args,
        with_structured_output: type[BaseModel] | None = None,
        store: BaseStore | None = None,
        fallback_model_name: str | None = None,
        **kwargs,
    ):
        self.tool_classes = tools
        self.with_structured_output = with_structured_output
        self.store = store
        self.structured_tool_name = None
        self.state_class: type[AgentState] = AgentState
        self.fallback_model_name = fallback_model_name
        if self.with_structured_output:
            self.tool_classes.append(self.with_structured_output)
            self.structured_tool_name = self.with_structured_output.model_json_schema()["title"]
            self.state_class = StructuredAgentState
        super().__init__(*args, **kwargs)

    def compile(self) -> CompiledStateGraph:
        """
        Compile the workflow for the agent.
        """
        workflow = StateGraph(self.state_class)

        workflow.add_node("agent", self.call_model)
        workflow.add_node("tools", ToolNode(self.tool_classes))
        if self.with_structured_output:
            workflow.add_node("respond", self.respond)

        workflow.set_entry_point("agent")

        path_map: dict[Hashable, str] = {"continue": "tools", "end": END}
        if self.with_structured_output:
            path_map["respond"] = "respond"

        workflow.add_conditional_edges("agent", self.should_continue, path_map)

        workflow.add_edge("tools", "agent")
        if self.with_structured_output:
            workflow.add_edge("respond", END)

        return workflow.compile(store=self.store)

    def call_model(self, state: AgentState):
        """
        Call the model with the current state.

        Args:
            state (AgentState): The current state of the agent.

        Returns:
            dict: The response from the model.
        """
        tools_kwargs = {}
        if self.with_structured_output:
            tools_kwargs = {"tool_choice": "auto"}

        llm_with_tools = self.model.bind_tools(self.tool_classes, **tools_kwargs)

        try:
            response = llm_with_tools.invoke(state["messages"])
        except (AnthropicInternalServerError, OpenAIInternalServerError) as e:
            if self.fallback_model_name:
                logger.warning(
                    "[ReAcT] Exception raised invoking model %s. Falling back to %s.",
                    self.model_name,
                    self.fallback_model_name,
                )
                llm_with_tools = self.get_model(model=self.fallback_model_name).bind_tools(
                    self.tool_classes, **tools_kwargs
                )
                response = llm_with_tools.invoke(state["messages"])
            else:
                raise e

        if isinstance(response, AIMessage) and response.tool_calls and state["is_last_step"]:
            response = AIMessage(id=response.id, content="I've reached the maximum number of steps.")
            logger.warning("[ReAcT] Last step reached. Ending the conversation.")

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

        last_message = cast("AIMessage", state["messages"][-1])

        response = None

        try:
            response = self.with_structured_output(**last_message.tool_calls[0]["args"])
        except ValidationError:
            logger.warning("[ReAcT] Error structuring output with tool args. Fallback to llm with_structured_output.")

            llm_with_structured_output = self.model.with_structured_output(
                self.with_structured_output, method="json_schema"
            )
            response = cast(
                "BaseModel",
                llm_with_structured_output.invoke(
                    [HumanMessage(last_message.pretty_repr())],
                    config={"configurable": {"model": settings.GENERIC_COST_EFFICIENT_MODEL_NAME}},
                ),
            )

        return {"response": response}

    def should_continue(self, state: AgentState) -> Literal["respond", "continue", "end"]:
        """
        Check if the agent should continue or end.

        Args:
            state (AgentState): The current state of the agent.

        Returns:
            str: The next step for the agent.
        """
        last_message = cast("AIMessage", state["messages"][-1])

        if (
            last_message.tool_calls
            and self.structured_tool_name
            and last_message.tool_calls[0]["name"] == self.structured_tool_name
        ):
            return "respond"
        elif not last_message.tool_calls:
            return "end"
        return "continue"


def prepare_repository_files_as_messages(
    repo_client: AllRepoClient, repo_id: str, ref: str, paths: list[str], store: BaseStore
) -> list[MessageLikeRepresentation]:
    """
    Prepare repository files as messages to preload them in agents to reduce their execution time.

    This is useful for agents that use plan and execute reasoning.

    Args:
        repo_client (AllRepoClient): The repository client.
        repo_id (str): The ID of the repository.
        ref (str): The reference of the repository.
        paths (list[str]): The paths of the files to preload.
        store (BaseStore): The used store for file changes.

    Returns:
        list[AIMessage | ToolMessage]: The messages to preload in agents.
    """
    retrieve_file_content_tool = RetrieveFileContentTool(
        source_repo_id=repo_id, source_ref=ref, api_wrapper=repo_client, return_not_found_message=False
    )

    tool_calls = []
    tool_call_messages = []

    for path in set(paths):
        if repository_file_content := retrieve_file_content_tool.invoke({
            "file_path": path,
            "intent": "Check current implementation",
            "store": store,
        }):
            tool_call_id = str(uuid.uuid4())
            tool_calls.append(
                ToolCall(
                    id=tool_call_id,
                    name=RETRIEVE_FILE_CONTENT_NAME,
                    args={"file_path": path, "intent": "Check current implementation"},
                )
            )
            tool_call_messages.append(ToolMessage(content=repository_file_content, tool_call_id=tool_call_id))

    return [
        AIMessage(
            content=textwrap.dedent(
                """\
                I'll help you execute these tasks precisely. Let's go through them step by step.

                <explanation>
                First, I'll examine the existing files to understand the current implementation and ensure our changes align with the codebase patterns. Let me retrieve the relevant files.
                </explanation>
                """  # noqa: E501
            ),
            tool_calls=tool_calls,
        ),
        *tool_call_messages,
    ]
