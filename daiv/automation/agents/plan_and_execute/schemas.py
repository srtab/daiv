from textwrap import dedent

from pydantic import BaseModel, ConfigDict, Field

DETERMINE_NEXT_ACTION_TOOL_NAME = "determine_next_action"


class HumanApproval(BaseModel):
    """
    Provide the result of the human approval analysis.
    """

    is_unambiguous_approval: bool = Field(description="Whether the response is an unambiguous approval.")
    approval_phrases: list[str] = Field(description="The phrases that indicate an unambiguous approval.")
    comments: str = Field(description="Additional comments or context regarding the approval.")
    feedback: str = Field(
        description=dedent(
            """\
            Human friendly feedback to the user about the approval.

            Examples:
            - Thanks for the approval, I'll apply the plan straight away.
            - I can't proceed until a clear approval of the presented plan. Please reply with a clear approval to proceed, or change issue details if the plan doesn't match your expectations.
            """  # noqa: E501
        )
    )


class AskForClarification(BaseModel):
    """
    Ask the human for clarification if their request is unclear.
    """

    # Need to add manually `additionalProperties=False` to allow use the schema
    # `DetermineNextAction` as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    questions: list[str] = Field(
        description="Questions phrased in the first person. E.g., 'Could you please clarify what you mean by...?'"
    )


class Task(BaseModel):
    """
    A detailed task to be executed by the AI agents.
    """

    # Need to add manually `additionalProperties=False` to allow use the schema
    # `DetermineNextAction` as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    title: str = Field(description="A title of the task to be executed by the AI agents.")
    context: str = Field(description="Additional context or information to help the AI agents understand the task.")
    subtasks: list[str] = Field(description="A list of subtasks to be executed in order.")
    path: str = Field(description="The path to the file where the task should be executed (if applicable).")


class Plan(BaseModel):
    """
    Outline future tasks to be addressed by the ai agents.
    """

    # Need to add manually `additionalProperties=False` to allow use the schema
    # `DetermineNextAction` as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    tasks: list[Task] = Field(description="A sorted list of tasks to follow.")
    goal: str = Field(description="A detailed objective of the requested changes to be made.")


class DetermineNextAction(BaseModel):
    """
    Respond with the appropriate action.

    Usage Guidelines:
     - Choose the appropriate action based on the feedback.
     - Communicate in the first person, as if speaking directly to the human.
     - Be clear, concise, and professional in your responses, tasks, or questions.
    """

    model_config = ConfigDict(title=DETERMINE_NEXT_ACTION_TOOL_NAME)

    action: Plan | AskForClarification = Field(description="The next action to be taken.")
