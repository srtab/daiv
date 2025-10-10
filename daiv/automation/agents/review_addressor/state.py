from typing import TYPE_CHECKING, Annotated

from langgraph.graph.message import add_messages
from langgraph.prebuilt.chat_agent_executor import AgentState
from typing_extensions import TypedDict

if TYPE_CHECKING:
    from langchain_core.messages import AnyMessage


class OverallState(TypedDict):
    """
    The state of the review addressor agent.
    """

    notes: Annotated[list[AnyMessage], add_messages]
    """
    The notes of the discussion left on the merge request.
    """

    diff: str
    """
    The unified diff of the merge request.
    """

    reply: str
    """
    The reply to show to the reviewer.

    It can be a direct reply to the comment left by the reviewer or questions from the plan and execute node to be
    clarified by the reviewer.
    """


class ReplyAgentState(AgentState):
    """
    Schema for the reply react agent.
    """

    diff: str
    """
    The unified diff of the merge request.
    """

    current_date_time: str
    """
    The current date and time.
    """
