from textwrap import dedent

from pydantic import BaseModel, Field


class FinalFeedback(BaseModel):
    """
    Use this tool to provide feedback to the user after the task is completed.

    Feedback should be used to:
     - question when less well-specified comments, where the user's requests are vague or incomplete;
     - provide feedback when the user's requests are clear and complete and no code changes are needed.
    """

    code_changes_needed: bool = Field(description="Whether code changes are needed to complete the task.")
    feedback: str = Field(
        description="Feedback to the user. Leave blank if code changes are needed or questions to make."
    )
    questions: list[str] = Field(
        description=dedent(
            """\
            Questions for the user to answer to help you complete the task. Leave empty if there are no questions.
            """
        )
    )

    @property
    def discussion_note(self):
        return self.questions and "\n".join(self.questions) or self.feedback
