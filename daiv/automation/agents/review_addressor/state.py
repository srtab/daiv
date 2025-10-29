from operator import add
from typing import Annotated

from langchain.agents import AgentState
from typing_extensions import TypedDict

from .schemas import ReviewContext  # noqa: TC001


class ReviewInState(TypedDict):
    """
    The review context to be reviewed.
    """

    to_review: list[ReviewContext]
    """
    The discussions to review.
    """


class ReplyReviewerState(TypedDict):
    """
    Schema for the reply reviewer agent.
    """

    review_context: ReviewContext
    """
    The review context to reply to.
    """


class OverallState(ReviewInState):
    to_reply: Annotated[list[ReviewContext], add]
    """
    The discussions to reply the reviewer for.
    """

    to_plan_and_execute: Annotated[list[ReviewContext], add]
    """
    The discussions to plan and execute the changes for.
    """


class ReplyAgentState(AgentState):
    """
    Schema for the reply react agent.
    """

    diff: str
    """
    The unified diff of the merge request.
    """
