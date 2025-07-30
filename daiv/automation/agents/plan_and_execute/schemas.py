from textwrap import dedent
from typing import Annotated

from langchain_core.messages import AnyMessage
from langchain_core.tools import InjectedToolCallId
from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypedDict


class HumanApprovalInput(TypedDict):
    """
    Provide the input for the human approval analysis.
    """

    messages: list[AnyMessage]


class HumanApprovalEvaluation(BaseModel):
    """
    Provide the result of the human approval analysis.
    """

    is_unambiguous_approval: bool = Field(description="Whether the response is an unambiguous approval.")
    approval_phrases: list[str] = Field(description="The phrases that indicate an unambiguous approval.")
    comments: str = Field(description="Additional comments or context regarding the approval.")
    feedback: str = Field(
        description=dedent(
            """\
            Use the same language as the user approval feedback.

            Examples (don't use these exact phrases, just use the same meaning):
            - Thanks for the approval, I'll apply the plan straight away.
            - I can't proceed until a clear approval of the presented plan. Please reply with a clear approval to proceed, or change issue details if the plan doesn't match your expectations.
            """  # noqa: E501
        )
    )


class AskForClarification(BaseModel):
    """
    Ask the user follow-up questions when their original request is ambiguous, incomplete, or self-contradictory.
    """

    # Need to add manually `additionalProperties=False` to allow use the schema
    # `DetermineNextAction` as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    questions: str = Field(
        description=dedent(
            """\
            The question(s) should be targeted and phrased **in the same language** as the user's request.
             - Provide at least one question and no superfluous chit-chat.
             - Use markdown formatting (e.g., for `variables`, `files`, `directories`, `dependencies`) as needed.
            """  # noqa: E501
        )
    )


class ChangeInstructions(BaseModel):
    """
    A single, self-contained description of what must change in the code-base.

    Each instance represents one atomic piece of work that a developer can tackle independently.
    If several edits are tightly coupled, group them in the same `ChangeInstructions` object and reference the
    shared file with `file_path`.
    """

    # Need to add manually `additionalProperties=False` to allow use the schema
    # `DetermineNextAction` as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    relevant_files: list[str] = Field(
        description=dedent(
            """\
            Every file path that a developer should open to implement this change (implementation, helpers, tests, docs, configs...). Include *all* files that provide necessary context.
            """  # noqa: E501
        )
    )
    file_path: str = Field(
        description=dedent(
            """\
            The primary file that will be modified.
             - Use an empty string ("") if the instruction is repository-wide or not tied to a single file (e.g., “add GitHub Action”).
             - Otherwise give the canonical path, relative to the repo root.
            """  # noqa: E501
        )
    )
    details: str = Field(
        description=dedent(
            """\
            A clear, human-readable explanation of the required change—algorithms, naming conventions, error handling, edge cases, test approach, performance notes, shell commands to run, etc.
             - **Do NOT** write or paste a full diff / complete implementation you have invented;
             - You **may** embed short illustrative snippets **or** verbatim user-supplied code **only if it is syntactically correct**. If the user's snippet contains errors, describe or reference it in prose instead of pasting the faulty code;
             - Use the safe format: fenced with tildes `~~~language` … `~~~` for markdown code blocks;
             - Use markdown formatting (e.g., for `variables`, `files`, `directories`, `dependencies`) as needed.
            """  # noqa: E501
        )
    )


class Plan(BaseModel):
    """
    A complete implementation plan that satisfies the user's request.

    The plan must be an ordered list of granular `ChangeInstructions`. Keep items in the order they should be executed.
    Related instructions affecting the same file should appear in consecutive order to aid batching and review.
    """

    # Need to add manually `additionalProperties=False` to allow use the schema
    # `DetermineNextAction` as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    changes: list[ChangeInstructions] = Field(
        description="Sorted so that related edits to the same file are adjacent.", min_length=1
    )


class CompleteWithPlan(BaseModel):
    """
    The plan to execute.
    """

    model_config = ConfigDict(title="complete_with_plan")

    tool_call_id: Annotated[str, InjectedToolCallId]
    plan: Plan = Field(
        description="The plan to execute.",
        examples=[{"changes": [{"file_path": "src/app.py", "relevant_files": ["src/app.py"], "details": "..."}]}],
    )


class CompleteWithClarification(BaseModel):
    """
    The question(s) to ask the user for clarification.
    """

    model_config = ConfigDict(title="complete_with_clarification")

    tool_call_id: Annotated[str, InjectedToolCallId]
    ask_for_clarification: AskForClarification = Field(
        description="The question(s) to ask the user for clarification.",
        examples=[{"questions": ["Could you clarify which database driver we must support?"]}],
    )


class CompleteWithPlanOrClarification(BaseModel):
    """
    The plan or question(s) to ask the user for clarification.
    """

    model_config = ConfigDict(title="complete_with_plan_or_clarification")

    tool_call_id: Annotated[str, InjectedToolCallId]
    action: Plan | AskForClarification = Field(description="The plan or question(s) to ask the user for clarification.")
