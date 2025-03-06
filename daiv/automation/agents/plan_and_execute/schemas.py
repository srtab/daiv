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

    questions: list[str] = Field(description="Questions phrased in the first person with clear and concise language.")


class Task(BaseModel):
    """
    A detailed task to be executed by the developer. REMEMBER: The developer will not have access to the user request.
    """

    # Need to add manually `additionalProperties=False` to allow use the schema
    # `DetermineNextAction` as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    title: str = Field(description="A title of the high-level task.")
    description: str = Field(
        description=dedent(
            """\
            Detailed description to help the developer understand the logic and implementation decisions.
            Dependencies / links between tasks should also be referenced here.
            You can use multiple paragraphs if needed.
            """  # noqa: E501
        )
    )
    subtasks: list[str] = Field(
        description=dedent(
            """\
            A list of subtasks to be executed in order. Be detailed and specific on what to do. The subtasks should be self-contained and executable on their own without further context. You can use multiple paragraphs.
            - You should NOT add subtasks to manage files (open, save, find, etc). Bad examples: "Open CHANGES.md file", "Save changes to CHANGES.md file", "Open CHANGES.md file", "Save changes to CHANGES.md file", "Find line x in CHANGES.md file" or variations of these.
            - You should NOT add subtasks to execute commands/tests. Bad examples: "Run the test suite", "Run tests to ensure coverage", "Run the linter...", "Run the formatter..." or variations of these.
            """  # noqa: E501
        )
    )
    path: str = Field(
        description="The path to the file where the task should be executed (if applicable). Otherwise, leave empty."
    )
    context_paths: list[str] = Field(
        description=dedent(
            """\
            A list of paths to files that are relevant to the task.
            The developer will use these files to understand the task and implement it.
            """  # noqa: E501
        )
    )


class Plan(BaseModel):
    """
    Outline future tasks and the goal to be addressed by the developer.
    """

    # Need to add manually `additionalProperties=False` to allow use the schema
    # `DetermineNextAction` as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    tasks: list[Task] = Field(description="A sorted list of tasks to follow.")
    goal: str = Field(description="A detailed objective of the requested changes to be made.")


class DetermineNextAction(BaseModel):
    """
    Respond with the appropriate action.
    """

    model_config = ConfigDict(title=DETERMINE_NEXT_ACTION_TOOL_NAME)

    tool_call_id: Annotated[str, InjectedToolCallId]
    action: Plan | AskForClarification = Field(description="The next action to be taken.")
