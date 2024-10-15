from pydantic import BaseModel, Field


class HumanFeedbackResponse(BaseModel):
    """
    The response to a human feedback.
    """

    is_unambiguous_approval: bool = Field(description="Whether the response is an unambiguous approval.")
    approval_phrases: list[str] = Field(description="The phrases that indicate an unambiguous approval.")
    comments: str = Field(description="Additional comments or context regarding the approval.")
    feedback: str = Field(description="The final feedback to provide to the human.")
