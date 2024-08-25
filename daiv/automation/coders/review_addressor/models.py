from textwrap import dedent

from pydantic import BaseModel, Field


class RequestFeedback(BaseModel):
    """
    This tool is used to ask for feedback if you are unable to address the comments or the comments are ambiguous.
    """

    questions: list[str] = Field(
        default_factory=list,
        description=dedent(
            """\
            Questions for the user to answer to help you complete the task. Leave empty if there are no questions.
            """
        ),
    )
    code_changes_needed: bool = Field(
        default=False, description="Whether code changes are needed to complete the task."
    )
