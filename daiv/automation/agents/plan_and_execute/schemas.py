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
            Every file path a developer should open to implement this change (implementation, helpers, tests, docs, configs...). Include ALL files that provide necessary and relevant context.
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
            Clear, concise, human-readable instructions covering the required change: affected symbols/APIs, algorithms, naming conventions, error handling, edge cases, test approach, performance notes, shell commands to run, etc.
             - IMPORTANT: **Do NOT** write or paste a full diff / complete implementation you have invented;
             - **Prefer** diff-like or path+pseudocode; include only key fragments.
             - You **may** embed verbatim user-supplied code **only if it is syntactically correct**. If the user's snippet contains errors, describe or reference it in prose instead of pasting the faulty code;
             - Use the safe format: fenced with tildes `~~~language` … `~~~` for markdown code blocks;
             - Use markdown formatting (e.g., for `variables`, `files`, `directories`, `dependencies`) as needed.
            """  # noqa: E501
        )
    )


class FinishOutput(BaseModel):
    """
    Deliver a concise summary of changes made to the repository.

    **Usage rules:**
    - Report only actual modifications made to the codebase.
    - Focus on observable changes: files, commands, installations.
    """

    aborting: bool = Field(default=False, description="Indicates if execution was aborted before completing")
    message: str = Field(
        description=dedent(
            """\
            Concise summary of changes made to the repository.

            **Include:**
            - Files modified, created, or deleted with brief description of changes
            - Commands executed (dependencies installed, scripts run)
            - Changes that failed and why (file not found, permission denied, command failed)
            - If aborted, state what prevented completion

            **Avoid:**
            - Internal workflow steps (plans, reviews, verification, analysis)
            - Meta-commentary about the process
            - Terms like "change plan", "code review", "requirements satisfied"

            Use markdown for `files`, `directories`, `variables`, `commands`. Be factual and specific.
            """  # noqa: E501
        )
    )


class PlanOutput(BaseModel):
    """
    Deliver a self-contained implementation plan that meets the user's requirements.

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
            "Group adjacent items when they affect the same file, including repository-wide changes."
        ),
        min_length=1,
    )


class ClarifyOutput(BaseModel):
    """
    Ask targeted questions to clarify the user's requirements.

    **Usage rules:**
    - Use when requirements are ambiguous or incomplete.
    - Ask only what's necessary to understand what changes to make.
    """

    type: Literal["clarify"] = Field(default="clarify", description="Type discriminator for clarify output")
    questions: str = Field(
        description=dedent(
            """\
            Targeted questions to clarify requirements.

            **Include:**
            - Reference specific files, functions, or code when relevant
            - Ask about concrete choices (which approach, which file, what behavior)
            - Present options when multiple valid interpretations exist

            **Avoid:**
            - Generic or vague questions
            - Asking about things already visible in the codebase
            - Mentioning internal processes (search, analysis, planning)

            Be direct and concise. Use markdown for `files`, `variables`, `functions`.
            """  # noqa: E501
        )
    )


class CompleteOutput(BaseModel):
    """
    Deliver a message confirming the repository already satisfies the requirements.

    **Usage rules:**
    - Use when the current state already meets what was requested.
    - Provide concrete evidence from the codebase.
    """

    type: Literal["complete"] = Field(default="complete", description="Type discriminator for complete output")
    message: str = Field(
        description=dedent(
            """\
            Explain why no changes are needed with specific evidence from the repository.

            **Include:**
            - State that the requirement is already satisfied
            - Cite specific evidence: files, code excerpts, configurations, dependencies
            - Reference exact locations (file paths, line numbers, function names)

            **Avoid:**
            - Mentioning investigation or analysis processes
            - Vague statements without concrete evidence
            - Internal workflow references

            Use markdown for `files`, `code`, `variables`. Keep code excerpts brief.
            """  # noqa: E501
        )
    )


# Discriminated union of all possible finalizer outputs
FinalizerOutput = PlanOutput | ClarifyOutput | CompleteOutput
