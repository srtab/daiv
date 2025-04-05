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


class ChangeInstructions(BaseModel):
    """
    Provide the instructions details.
    """

    # Need to add manually `additionalProperties=False` to allow use the schema
    # `DetermineNextAction` as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    relevant_files: list[str] = Field(
        description=dedent(
            """\
            The paths to the files that are relevant to complete this instructions.
            """  # noqa: E501
        )
    )
    file_path: str = Field(
        description=dedent(
            """\
            The path to the file where the instructions should be applied, if applicable.
            If the instructions are not related to a specific file, leave this empty.
            """  # noqa: E501
        )
    )
    details: str = Field(
        description=dedent(
            """\
            It's important to share the algorithm you've thought of that should be followed, and to apply identified conventions on the details (don't just say: “follow the project's testing convention”, reflect them on the changes details).
            Use a human readable language, describing the changes to be made using natural language, not the code implementation. You can use multiple paragraphs to describe the changes to be made.
            """  # noqa: E501
        )
    )


class Plan(BaseModel):
    """
    Outline the plan of changes/instructions to apply to the codebase to address the user request.
    """

    # Need to add manually `additionalProperties=False` to allow use the schema
    # `DetermineNextAction` as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    goal: str = Field(description="A detailed objective of the requested changes to be made.")
    changes: list[ChangeInstructions] = Field(
        description=dedent(
            """\
            A sorted list of changes/instructions to apply to the codebase. Group related changes by file_path whenever possible and applyable.
            """  # noqa: E501
        )
    )


class DetermineNextAction(BaseModel):
    """
    Respond with the appropriate action.
    """

    model_config = ConfigDict(title=DETERMINE_NEXT_ACTION_TOOL_NAME)

    tool_call_id: Annotated[str, InjectedToolCallId]
    action: Plan | AskForClarification = Field(description="The next action to be taken.")
