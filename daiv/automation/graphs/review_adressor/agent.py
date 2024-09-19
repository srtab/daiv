import logging
from typing import Literal

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langchain_core.output_parsers.openai_tools import PydanticToolsParser
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode

from automation.graphs.agents import BaseAgent
from automation.tools import CodebaseSearchTool
from codebase.clients import AllRepoClient
from codebase.indexes import CodebaseIndex

from .prompts import review_analyzer_system
from .schemas import FinalFeedback
from .state import OverallState

logger = logging.getLogger(__name__)


class ReviewAdressorAgent(BaseAgent):
    """
    Agent to address reviews by providing feedback and asking questions.
    """

    model_name = "gpt-4o-2024-08-06"

    def __init__(
        self,
        repo_client: AllRepoClient,
        *,
        source_repo_id: str,
        source_ref: str,
        merge_request_id: int,
        discussion_id: str,
    ):
        self.repo_client = repo_client
        self.source_repo_id = source_repo_id
        self.source_ref = source_ref
        self.merge_request_id = merge_request_id
        self.discussion_id = discussion_id
        self.inspect_tools = [
            CodebaseSearchTool(source_repo_id=source_repo_id, api_wrapper=CodebaseIndex(repo_client=repo_client))
        ]
        super().__init__()
        self.model_with_inspect_tools = self.model.bind_tools(
            self.inspect_tools + [FinalFeedback], tool_choice="required"
        )

    def compile(self) -> CompiledStateGraph:
        # Create the workflow
        workflow = StateGraph(OverallState)

        # Add nodes
        workflow.add_node("notes_to_messages", self.notes_to_messages)
        workflow.add_node("reviewer", self.reviewer)
        workflow.add_node("request_feedback", self.request_feedback)
        workflow.add_node("inspect_tools", ToolNode(self.inspect_tools))
        workflow.add_node("plan_code_changes", self.plan_code_changes)

        # Add edges
        workflow.add_edge(START, "notes_to_messages")
        workflow.add_edge("notes_to_messages", "reviewer")
        workflow.add_conditional_edges(
            "reviewer", self.should_continue_inspect, ["inspect_tools", "request_feedback", "plan_code_changes"]
        )
        workflow.add_edge("inspect_tools", "reviewer")
        workflow.add_edge("request_feedback", END)

        return workflow.compile(interrupt_after=["request_feedback"])

    def notes_to_messages(self, state: OverallState):
        """
        Convert notes to messages to thread the conversation.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            dict: The state of the agent to update.
        """
        messages: list[AnyMessage] = []

        for note in state["notes"]:
            if note.author.id == self.repo_client.current_user.id:
                messages.append(AIMessage(content=note.body))
            else:
                messages.append(HumanMessage(content=note.body))

        return {"messages": messages}

    def reviewer(self, state: OverallState):
        """
        Review the notes left on the provided unidiff.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            dict: The state of the agent to update.
        """
        system_message = review_analyzer_system.format(diff=state["diff"])
        return {"messages": [self.model_with_inspect_tools.invoke([SystemMessage(system_message)] + state["messages"])]}

    def should_continue_inspect(
        self, state: OverallState
    ) -> Literal["inspect_tools", "request_feedback", "plan_code_changes"]:
        """
        Determine if the agent should continue inspecting tools, request feedback, or plan code changes.

        Args:
            state (OverallState): The state of the agent.

        Returns:
            str: The next state to transition to.
        """
        tool_calls = state["messages"][-1].tool_calls

        if not tool_calls:
            return END

        elif any(tool_call["name"] == FinalFeedback.__name__ for tool_call in tool_calls):
            response: FinalFeedback = PydanticToolsParser(tools=[FinalFeedback], first_tool_only=True).invoke(
                state["messages"][-1]
            )
            if response.questions or (response.feedback and not response.code_changes_needed):
                return "request_feedback"
            elif response.code_changes_needed:
                return "plan_code_changes"
        return "inspect_tools"

    def request_feedback(self, state: OverallState):
        """
        Request feedback from the user.

        Args:
            state (OverallState): The state of the agent.
        """
        response: FinalFeedback = PydanticToolsParser(tools=[FinalFeedback], first_tool_only=True).invoke(
            state["messages"][-1]
        )
        self.repo_client.create_merge_request_discussion_note(
            self.source_repo_id, self.merge_request_id, self.discussion_id, response.discussion_note
        )

    def plan_code_changes(self, state: OverallState):
        pass
