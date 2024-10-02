from textwrap import dedent

from pydantic import BaseModel, ConfigDict, Field

ACT_RESPONSE_TOOL_NAME = "act"


class Plan(BaseModel):
    """
    Plan to follow in future
    """

    tasks: list[str] = Field(description="different tasks to follow, should be in sorted order")
    goal: str = Field(description="detailed objective of the requested changes to be made")


class Response(BaseModel):
    """
    Final response to reviewer if the reviewer ask a question or no more work need to be done.
    """

    response: str = Field(
        description=dedent(
            """\
            Just answers without asking if he wants to do something more.
            Answer in the first person, e.g. 'The changes you requested have been made', as if you were speaking directly to the reviewer.
            """  # noqa: E501
        )
    )
    finished: bool = Field(description="If the task has been completed, set to True, otherwise False.")


class AskForClarification(BaseModel):
    """
    If the reviewer request is not clear, use this to ask the reviewer for clarification to help you complete the task.
    """

    questions: list[str] = Field(
        description=dedent(
            """\
            Ask in the first person, e.g. 'Can you provide more details?', as you where speaking directly to the reviewer.
            """  # noqa: E501
        )
    )


class ActPlannerResponse(BaseModel):
    """
    Use this tool to respond to the reviewer with the proper action to take.
    """

    model_config = ConfigDict(title=ACT_RESPONSE_TOOL_NAME)

    action: Response | Plan | AskForClarification = Field(description="Next action to be taken.")


class ActExecuterResponse(BaseModel):
    """
    Use this tool to respond to the reviewer with the proper action.
    """

    model_config = ConfigDict(title=ACT_RESPONSE_TOOL_NAME)

    action: Response | AskForClarification = Field(description="Next action to be taken.")
