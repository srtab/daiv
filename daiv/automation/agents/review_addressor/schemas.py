from textwrap import dedent

from pydantic import BaseModel, Field


class ReviewAssessment(BaseModel):
    """
    This tool is intended to be used to respond the result of the classification assessment whether a feedback
    is a request for direct changes to the codebase, and provide the rationale behind that classification.
    """

    # this is commented to avoid the error: https://github.com/langchain-ai/langchain/issues/27260#issue-2579527949
    # model_config = ConfigDict(title="review_assessment")  # noqa: ERA001

    request_for_changes: bool = Field(description="True if classified as a 'Change Request', and false otherwise.")
    justification: str = Field(description="Brief explanation of your reasoning for the classification.")
    requested_changes: list[str] = Field(
        description=(
            "Describe what changes where requested in a clear, concise and actionable way. "
            "If no changes are requested, return an empty list. "
            "Be as verbose as possible to avoid lost important information."
        ),
        default_factory=list,
    )


class ReplyReviewer(BaseModel):
    """
    Provide a reply to the reviewer's comment or question.
    """

    reply: str = Field(
        description=dedent(
            """\
            - The reply MUST be under 100 words.
            - Format your response using appropriate markdown for code snippets, lists, or emphasis where needed.
            """
        )
    )
