from pydantic import BaseModel, ConfigDict, Field

DETERMINE_NEXT_ACTION_TOOL_NAME = "determine_next_action"


class RequestAssessmentResponse(BaseModel):
    """
    Respond to the human feedback with an assessment of the requested changes.
    """

    # this is commented to avoid the error: https://github.com/langchain-ai/langchain/issues/27260#issue-2579527949
    # model_config = ConfigDict(title="request_assessment")  # noqa: ERA001

    request_for_changes: bool = Field(description="Set to True if the human requested changes; otherwise, False.")
    justification: str = Field(description="Justify why you think it's a change request.")


class AskForClarification(BaseModel):
    """
    Ask the human for clarification if their request is unclear.
    """

    # Need to add manually `additionalProperties=False` to allow use the schema
    # `DetermineNextActionResponse` as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    questions: list[str] = Field(
        description="Questions phrased in the first person. E.g., 'Could you please clarify what you mean by...?'"
    )


class Plan(BaseModel):
    """
    Outline future tasks to be addressed by the ai agents.
    """

    # Need to add manually `additionalProperties=False` to allow use the schema
    # `DetermineNextActionResponse` as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    tasks: list[str] = Field(description="A sorted list of tasks to follow.")
    goal: str = Field(description="A detailed objective of the requested changes to be made.")


class DetermineNextActionResponse(BaseModel):
    """
    Respond with the appropriate action.

    Usage Guidelines:
     - Choose the appropriate action based on the feedback.
     - Communicate in the first person, as if speaking directly to the human.
     - Be clear, concise, and professional in your responses, tasks, or questions.
    """

    model_config = ConfigDict(title=DETERMINE_NEXT_ACTION_TOOL_NAME)

    action: Plan | AskForClarification = Field(description="The next action to be taken.")
