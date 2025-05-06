from langchain_core.messages import AnyMessage
from pydantic import BaseModel, Field
from typing_extensions import TypedDict


class ReviewCommentInput(TypedDict):
    """
    Provide the input for the review comment evaluator.
    """

    messages: list[AnyMessage]


class ReviewCommentEvaluation(BaseModel):
    """
    This tool is intended to be used to respond the result of the classification assessment whether a feedback
    is a request for direct changes to the codebase, and provide the rationale behind that classification.
    """

    # this is commented to avoid the error: https://github.com/langchain-ai/langchain/issues/27260#issue-2579527949
    # model_config = ConfigDict(title="review_assessment")  # noqa: ERA001

    request_for_changes: bool = Field(description="True if classified as a 'Change Request', and false otherwise.")
    justification: str = Field(description="Brief explanation of your reasoning for the classification.")
