from __future__ import annotations

from textwrap import dedent
from typing import Literal

from pydantic import BaseModel, Field


class ChangeInstructions(BaseModel):
    """
    One atomic piece of work a developer can tackle independently.
    If several edits are tightly coupled, group them in the same object and reference the shared `file_path`.
    """

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


class PlanOutput(BaseModel):
    """
    Deliver a self-contained implementation plan that satisfies the user's request.

    **Usage rules:**
    - The requirements are clear and changes are needed.
    - The context is sufficient to deliver the plan with confidence.

    Requirements for the plan:
    - Ordered list of granular ChangeInstructions in execution order.
    - Related instructions that touch the same file appear consecutively to aid batching/review.
    - Self-contained: no external URLs; embed essential snippets/data (short snippets only) using safe fences.
    - Reference concrete files/functions/config keys discovered during inspection.
    """

    type: Literal["plan"] = Field(default="plan", description="Type discriminator for plan output")
    changes: list[ChangeInstructions] = Field(
        description=(
            "List of ChangeInstructions in the order they should be executed. "
            "Group adjacent items when they affect the same file."
        ),
        min_length=1,
    )


class ClarifyOutput(BaseModel):
    """
    Deliver targeted grounded questions to clarify the user's intent.

    **Usage rules:**
    - There's uncertainty about the requirements/changes needed.
    - The context is insufficient to deliver the plan with the user's intent with confidence.
    - The user needs to provide additional details that could not be covered by the context.
    """

    type: Literal["clarify"] = Field(default="clarify", description="Type discriminator for clarify output")
    questions: str = Field(
        description=dedent(
            """\
            Targeted concise, direct and to the point questions. No chit-chat. Ground them in the codebase and search results; use markdown formatting for `variables`, `files`, `directories`, `dependencies` as needed.
            """  # noqa: E501
        )
    )


class CompleteOutput(BaseModel):
    """
    Deliver a message to confirm no changes or actions are needed.

    **Usage rules:**
    - The context is sufficient to confirm no changes or actions are needed.
    - The current state meets the requirements.
    """

    type: Literal["complete"] = Field(default="complete", description="Type discriminator for complete output")
    message: str = Field(
        description="The message to demonstrate how current state meets requirements with specific evidence."
    )


# Discriminated union of all possible finalizer outputs
FinalizerOutput = PlanOutput | ClarifyOutput | CompleteOutput
