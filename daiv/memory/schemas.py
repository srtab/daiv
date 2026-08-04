from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# Static mirror of ``memory.models.ObservationCategory.values`` — declared explicitly (not derived) so
# pydantic and ty see the allowed values directly. Kept in sync with the model by
# ``tests.unit_tests.memory.test_models.test_observation_category_literal_matches_model_choices``.
ObservationCategoryLiteral = Literal["build_test", "codebase_fact", "pitfall", "reviewer_preference", "workflow"]


class ExtractedObservation(BaseModel):
    """A single observation extracted from a run transcript."""

    category: ObservationCategoryLiteral = Field(description="The kind of learning this observation captures.")
    content: str = Field(
        min_length=10,
        max_length=500,
        description=(
            "One specific, self-contained, verifiable fact useful in a future session on this repository. "
            "Plain text, one or two sentences, understandable without the transcript."
        ),
    )


class ExtractedObservations(BaseModel):
    """Structured output for the extraction pass."""

    observations: list[ExtractedObservation] = Field(
        default_factory=list,
        max_length=10,
        description="0-10 observations. An empty list is the expected output when the run taught nothing new.",
    )


MemoryOperationLiteral = Literal["ADD", "UPDATE", "MERGE", "CONFIRM", "DISCARD"]


class MemoryOperation(BaseModel):
    """A single targeted change to the repository's memory entries.

    Deliberately flat with permissive fields: which of them an operation requires depends on
    ``op``, and enforcing that structurally (unions, per-op models) inflates structured-output
    failure rates. Shape and reference validity are checked at apply time instead.
    """

    op: MemoryOperationLiteral = Field(
        description=(
            "ADD: create a new entry from the observations. "
            "UPDATE: replace exactly one existing entry with corrected/expanded content. "
            "MERGE: replace two or more existing entries of the SAME category with one combined entry. "
            "CONFIRM: the observations restate an existing entry, nothing to change. "
            "DISCARD: the observations are not worth keeping."
        )
    )
    entry_ids: list[str] = Field(
        default_factory=list,
        description=(
            "IDs of existing entries this operation targets, copied verbatim from the entry list: "
            "exactly one for UPDATE and CONFIRM, two or more for MERGE, empty for ADD and DISCARD."
        ),
    )
    observation_ids: list[str] = Field(
        default_factory=list,
        description=(
            "IDs of the new observations that motivated this operation, copied verbatim from the "
            "observation list. Every operation must name at least one."
        ),
    )
    category: ObservationCategoryLiteral | None = Field(
        default=None,
        description=(
            "Category of the entry being created. Required for ADD. Ignored elsewhere — a MERGE "
            "inherits the category of the entries it combines."
        ),
    )
    content: str | None = Field(
        default=None,
        max_length=500,
        description=(
            "The new entry's full text for ADD, UPDATE and MERGE — self-contained, specific, plain "
            "text, one or two sentences. Leave empty for CONFIRM and DISCARD."
        ),
    )
    reason: str | None = Field(
        default=None,
        description=(
            "Short justification for throwing these observations away. Required for DISCARD — an "
            "operation that drops a learning must say why — and ignored elsewhere."
        ),
    )


class MemoryOperations(BaseModel):
    """Structured output for the consolidation pass."""

    operations: list[MemoryOperation] = Field(
        default_factory=list,
        max_length=50,
        description="The operations to apply to this repository's memory. Every observation should be covered by one.",
    )
