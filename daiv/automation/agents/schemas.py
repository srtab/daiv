from pydantic import BaseModel, ConfigDict, Field

DETERMINE_NEXT_ACTION_TOOL_NAME = "determine_next_action"


class AssesmentClassificationResponse(BaseModel):
    """
    Use this tool to respond to the result of the assessment by classifying whether a feedback is a request for
    direct changes to the codebase, and provide the rationale behind that classification.
    """

    # this is commented to avoid the error: https://github.com/langchain-ai/langchain/issues/27260#issue-2579527949
    # model_config = ConfigDict(title="request_assessment")  # noqa: ERA001

    request_for_changes: bool = Field(description="True if classified as a 'Change Request', and false otherwise.")
    justification: str = Field(description="Brief explanation of your reasoning for the classification.")


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


class Task(BaseModel):
    """
    A detailed task to be executed by the AI agents.
    """

    # Need to add manually `additionalProperties=False` to allow use the schema
    # `DetermineNextActionResponse` as tool with strict mode
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
    # `DetermineNextActionResponse` as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    tasks: list[Task] = Field(description="A sorted list of tasks to follow.")
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
