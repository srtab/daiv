from __future__ import annotations

from textwrap import dedent

from pydantic import BaseModel, ConfigDict, Field

PLAN_DESCRIPTION = """\
Deliver a self-contained implementation plan that satisfies the user's request.

Requirements for the plan:
- Ordered list of granular ChangeInstructions in execution order.
- Related instructions that touch the same file appear consecutively to aid batching/review.
- Self-contained: no external URLs; embed essential snippets/data (short snippets only) using safe fences.
- Reference concrete files/functions/config keys discovered during inspection."""  # noqa: E501


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


Plan.__doc__ = PLAN_DESCRIPTION
