from pydantic import BaseModel, Field


class IssueAssessment(BaseModel):
    """
    This tool is intended to be used to respond the result of the classification assessment whether a issue
    is a request for direct changes to the codebase, and provide the rationale behind that classification.
    """

    # this is commented to avoid the error: https://github.com/langchain-ai/langchain/issues/27260#issue-2579527949
    # model_config = ConfigDict(title="issue_assessment")  # noqa: ERA001

    request_for_changes: bool = Field(description="True if classified as a 'Change Request', and false otherwise.")
    justification: str = Field(description="Brief explanation of your reasoning for the classification.")


class HumanFeedbackResponse(BaseModel):
    """
    The response to a human feedback.
    """

    is_unambiguous_approval: bool = Field(description="Whether the response is an unambiguous approval.")
    approval_phrases: list[str] = Field(description="The phrases that indicate an unambiguous approval.")
    comments: str = Field(description="Additional comments or context regarding the approval.")
    feedback: str = Field(description="The final feedback to provide to the human.")
