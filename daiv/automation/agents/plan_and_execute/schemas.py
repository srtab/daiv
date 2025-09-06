from __future__ import annotations

from textwrap import dedent
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field
from typing_extensions import TypedDict

from automation.agents.schemas import Image  # noqa: TC001
from automation.agents.tools.navigation import NAVIGATION_TOOLS

if TYPE_CHECKING:
    from langchain_core.messages import AnyMessage


FINALIZE_WITH_PLAN_DESCRIPTION = f"""\
FINALIZER — Deliver a self-contained implementation plan that satisfies the user's request.

Call this ONLY after completing Steps 0-1. Preconditions you MUST have satisfied earlier in this conversation:
(1) you have called `think` at least once in Step 0, and
(2) you have executed ≥1 inspection tool from {", ".join(NAVIGATION_TOOLS)} to gather evidence.
If either is false, do NOT call this tool; instead continue the workflow or use `post_inspection_clarify_final` if ambiguity remains after inspection.

Requirements for the plan:
- Ordered list of granular ChangeInstructions in execution order.
- Related instructions that touch the same file appear consecutively to aid batching/review.
- Self-contained: no external URLs; embed essential snippets/data (short snippets only) using safe fences.
- Reference concrete files/functions/config keys discovered during inspection."""  # noqa: E501

FINALIZE_WITH_TARGETED_QUESTIONS_DESCRIPTION = f"""\
FINALIZER — targeted clarification questions asked ONLY after completing Steps 0-1.

Preconditions you MUST have satisfied earlier in this conversation:
(1) you have called `think` at least once in Step 0, and
(2) you have executed ≥1 inspection tool from {", ".join(NAVIGATION_TOOLS)} attempting to resolve the ambiguity.
If either is false, do NOT call this tool.

Use this tool when ambiguity remains after inspection, when any required execution detail is still missing, or when external sources are conflicting."""  # NOQA: E501


class ImageURLExtractorOutput(BaseModel):
    images: list[Image] = Field(description="List of images found in the task.")


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
    # Need to add manually `additionalProperties=False` to allow use the schema  as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    questions: str = Field(
        description=dedent(
            """\
            Targeted questions in the same language as the user's request. No chit-chat. Ground them in the codebase and inspection results; use markdown formatting for `variables`, `files`, `directories`, `dependencies` as needed.
            """  # noqa: E501
        )
    )


AskForClarification.__doc__ = FINALIZE_WITH_TARGETED_QUESTIONS_DESCRIPTION


class ChangeInstructions(BaseModel):
    """
    One atomic piece of work a developer can tackle independently.
    If several edits are tightly coupled, group them in the same object and reference the shared `file_path`.
    """

    # Need to add manually `additionalProperties=False` to allow use the schema  as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    relevant_files: list[str] = Field(
        description=dedent(
            """\
            Every file path a developer should open to implement this change (implementation, helpers, tests, docs, configs...). Include ALL files that provide necessary context.
            """  # noqa: E501
        )
    )
    file_path: str = Field(
        description=dedent(
            """\
            Primary file to be modified. Use an empty string ("") if the instruction is repository-wide (e.g., 'add CI workflow'). Otherwise use the canonical path relative to repo root.
            """  # noqa: E501
        )
    )
    details: str = Field(
        description=dedent(
            """\
            Clear, human-readable instructions covering the required change: affected symbols/APIs, algorithms, naming conventions, error handling, edge cases, test approach, performance notes, shell commands to run, etc.
             - **Do NOT** write or paste a full diff / complete implementation you have invented;
             - You **may** embed short illustrative snippets **or** verbatim user-supplied code **only if it is syntactically correct**. If the user's snippet contains errors, describe or reference it in prose instead of pasting the faulty code;
             - Use the safe format: fenced with tildes `~~~language` … `~~~` for markdown code blocks;
             - Use markdown formatting (e.g., for `variables`, `files`, `directories`, `dependencies`) as needed.
            """  # noqa: E501
        )
    )


class Plan(BaseModel):
    # Need to add manually `additionalProperties=False` to allow use the schema  as tool with strict mode
    model_config = ConfigDict(json_schema_extra={"additionalProperties": False})

    changes: list[ChangeInstructions] = Field(
        description=(
            "List of ChangeInstructions in the order they should be executed. "
            "Group adjacent items when they affect the same file."
        ),
        min_length=1,
    )


Plan.__doc__ = FINALIZE_WITH_PLAN_DESCRIPTION
