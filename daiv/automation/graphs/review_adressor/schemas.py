from textwrap import dedent

from pydantic import BaseModel, ConfigDict, Field

DETERMINE_NEXT_ACTION_TOOL_NAME = "determine_next_action"


class HumanFeedbackResponse(BaseModel):
    """
    Provide a final response to the reviewer.
    """

    response: str = Field(
        description=dedent(
            """\
            Answer in the first person, without asking if they want more changes. E.g., "The changes you requested have been made."
            """  # noqa: E501
        )
    )


class AskForClarification(BaseModel):
    """
    Ask the reviewer for clarification if their request is unclear.
    """

    # Need to add manually `additionalProperties=False` to allow use the schema
    # `DetermineNextActionResponse` as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    questions: list[str] = Field(
        description="Questions phrased in the first person. E.g., 'Could you please clarify what you mean by...?'"
    )


class Plan(BaseModel):
    """
    Outline future tasks to address the reviewer's feedback.
    """

    # Need to add manually `additionalProperties=False` to allow use the schema
    # `DetermineNextActionResponse` as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    tasks: list[str] = Field(description="A sorted list of tasks to follow.")
    goal: str = Field(description="A detailed objective of the requested changes to be made.")


class DetermineNextActionResponse(BaseModel):
    """
    Respond to the reviewer with the appropriate action.

    Usage Guidelines:
     - Choose the appropriate action based on the reviewer's feedback.
     - Communicate in the first person, as if speaking directly to the reviewer.
     - Be clear, concise, and professional in your responses, tasks, or questions.
    """

    model_config = ConfigDict(title=DETERMINE_NEXT_ACTION_TOOL_NAME)

    action: Plan | AskForClarification = Field(description="The next action to be taken.")


class RequestAssessmentResponse(BaseModel):
    """
    Output schema for the `RequestForChanges` tool.
    """

    model_config = ConfigDict(title="request_assessment")

    request_for_changes: bool = Field(description="Set to True if the reviewer requested changes; otherwise, False.")
    justification: str = Field(description="Justify why you think it's a change request.")
