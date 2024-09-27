from textwrap import dedent

from pydantic import BaseModel, Field


class Plan(BaseModel):
    """Plan to follow in future"""

    tasks: list[str] = Field(description="different tasks to follow, should be in sorted order")


class InitialPlan(Plan):
    """Plan to follow in future"""

    goal: str = Field(description="detailed objective of the requested changes to be made")


class Response(BaseModel):
    """Final response to user."""

    response: str
    finished: bool = Field(description="If the task has been completed, set to True, otherwise False.")


class AskForClarification(BaseModel):
    """Questions for the user to answer to help you complete the task."""

    questions: list[str] = Field(
        description=dedent(
            """\
            Ask in first person, e.g. 'Can you provide more details?' as you where talking directly to the reviewer.
            """
        )
    )


class Act(BaseModel):
    """Action to perform."""

    action: Response | Plan | AskForClarification = Field(
        description="Action to perform. If you want to respond to user, use Response. "
        "If you want to ask the user for clarification, use AskForClarification. "
        "If you need to further use tools to get the answer, use Plan."
    )


class InitialAct(BaseModel):
    """Action to perform."""

    action: Response | InitialPlan | AskForClarification = Field(
        description="Action to perform. If you want to respond to user, use Response. "
        "If you want to ask the user for clarification, use AskForClarification. "
        "If you need to further use tools to get the answer, use Plan."
    )
