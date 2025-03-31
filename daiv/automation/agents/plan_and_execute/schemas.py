from textwrap import dedent
from typing import Annotated

from langchain_core.tools import InjectedToolCallId
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
            Human friendly feedback to the developer about the approval.

            Examples:
            - Thanks for the approval, I'll apply the plan straight away.
            - I can't proceed until a clear approval of the presented plan. Please reply with a clear approval to proceed, or change issue details if the plan doesn't match your expectations.
            """  # noqa: E501
        )
    )


class AskForClarification(BaseModel):
    """
    Ask for clarification to the user if the user request is unclear, missing data or ambiguous.
    """

    # Need to add manually `additionalProperties=False` to allow use the schema
    # `DetermineNextAction` as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    questions: list[str] = Field(description="Questions phrased in the same language as the user request.")


class CodeChanges(BaseModel):
    """
    A sorted list with the details of the changes to apply to a specific file on the codebase.
    """

    # Need to add manually `additionalProperties=False` to allow use the schema
    # `DetermineNextAction` as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    path: str = Field(description="The path to the file where the changes should be applied.")
    details: str = Field(
        description=dedent(
            """\
            All the modifications details that need to be done to address the user request in the format of instructions.
            """  # noqa: E501
        )
    )


class Plan(BaseModel):
    """
    Outline the goal to be addressed and the changes to apply to the codebase.
    """

    # Need to add manually `additionalProperties=False` to allow use the schema
    # `DetermineNextAction` as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    goal: str = Field(description="A detailed objective of the requested changes to be made.")
    changes: list[CodeChanges] = Field(description="A sorted list of changes to apply to the codebase.")


class DetermineNextAction(BaseModel):
    """
    Respond with the appropriate action.
    """

    model_config = ConfigDict(title=DETERMINE_NEXT_ACTION_TOOL_NAME)

    tool_call_id: Annotated[str, InjectedToolCallId]
    action: Plan | AskForClarification = Field(description="The next action to be taken.")
