from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

# Static mirror of ``memory.models.ObservationCategory.values`` — declared explicitly (not derived) so
# pydantic and ty see the allowed values directly. Kept in sync with the model by
# ``tests.unit_tests.memory.test_models.test_observation_category_literal_matches_model_choices``.
ObservationCategoryLiteral = Literal["build_test", "codebase_fact", "pitfall", "reviewer_preference", "workflow"]

CONTENT_HARD_LIMIT = 2000
MIN_CONTENT_CHARS = 10
# Prompt guidance only, stated to the model in ``memory.prompts``; nothing truncates on it.
MAX_OBSERVATIONS = 10
# Applied by truncation in ``run_consolidation_round``.
MAX_OPERATIONS = 50
CONTENT_GUIDELINE_CHARS = 500

# Normalisation, not validation: ``str.strip`` cannot fail, so unlike a length constraint it can
# never fail the payload — and the value checked is then the same one that gets stored.
StrippedContent = Annotated[str, AfterValidator(str.strip)]


def _content_error(content: str) -> str | None:
    """Why this content is unusable on length grounds, or ``None`` when it is within bounds.

    ``CONTENT_HARD_LIMIT`` is a runaway-generation fence far above the ``CONTENT_GUIDELINE_CHARS``
    the prompt asks for; never express either as a pydantic field constraint — see AGENTS.md
    §"Repository memory".
    """
    if len(content) < MIN_CONTENT_CHARS:
        return f"content is shorter than {MIN_CONTENT_CHARS} characters"
    if len(content) > CONTENT_HARD_LIMIT:
        return f"content is {len(content)} characters, over the hard limit"
    return None


class ExtractedObservation(BaseModel):
    """A single observation extracted from a run transcript."""

    category: ObservationCategoryLiteral = Field(description="The kind of learning this observation captures.")
    content: StrippedContent = Field(
        description=(
            "One specific, self-contained, verifiable fact useful in a future session on this repository. "
            f"Plain text, one or two sentences of at most ~{CONTENT_GUIDELINE_CHARS} characters, "
            "understandable without the transcript."
        )
    )

    def shape_error(self) -> str | None:
        """Why this observation is not worth storing, or ``None`` when it is."""
        return _content_error(self.content)


class ExtractedObservations(BaseModel):
    """Structured output for the extraction pass."""

    observations: list[ExtractedObservation] = Field(
        default_factory=list,
        description=(
            f"0-{MAX_OBSERVATIONS} observations. An empty list is the expected output when the run taught nothing new."
        ),
    )


MemoryOperationLiteral = Literal["ADD", "UPDATE", "MERGE", "CONFIRM", "DISCARD"]


class MemoryOperation(BaseModel):
    """A single targeted change to the repository's memory entries.

    Deliberately flat with permissive fields: which of them an operation requires depends on
    ``op``, and enforcing that structurally (unions, per-op models) inflates structured-output
    failure rates. Reference validity is checked at apply time; self-consistency is ``shape_error()`` below.
    """

    # Frozen so ``_dedupe_ids`` cannot be bypassed: field validators do not run on assignment, so a
    # mutable operation could reintroduce the duplicate ids that ``shape_error``'s arity checks trust.
    model_config = ConfigDict(frozen=True)

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
    content: StrippedContent | None = Field(
        default=None,
        description=(
            "The new entry's full text for ADD, UPDATE and MERGE — self-contained, specific, plain "
            f"text, one or two sentences of at most ~{CONTENT_GUIDELINE_CHARS} characters. "
            "Leave empty for CONFIRM and DISCARD."
        ),
    )
    reason: str | None = Field(
        default=None,
        description=(
            "Short justification for throwing these observations away. Required for DISCARD — an "
            "operation that drops a learning must say why — and ignored elsewhere."
        ),
    )

    @field_validator("entry_ids", "observation_ids")
    @classmethod
    def _dedupe_ids(cls, ids: list[str]) -> list[str]:
        """Order-preserving dedup so arity checks and the apply phase see the same targets.

        A repeated id in a MERGE would otherwise satisfy "two or more" and supersede one entry twice.
        """
        return list(dict.fromkeys(ids))

    def shape_error(self) -> str | None:
        """Why this operation is internally inconsistent, or ``None`` when it is well-formed.

        Self-consistency only: reference validity and the MERGE same-category fence need the
        round's entry snapshot and live in :class:`memory.consolidation.ConsolidationRound`.
        """
        if not self.observation_ids:
            return "references no observation"

        content = self.content or ""
        match self.op:
            case "ADD":
                if self.entry_ids:
                    return "ADD must not target existing entries"
                if not content:
                    return "ADD without content"
                # Pydantic's literal already rejected any non-empty value outside the choices.
                if self.category is None:
                    return "ADD without a category"
            case "UPDATE":
                if len(self.entry_ids) != 1:
                    return f"UPDATE must target exactly one entry, got {len(self.entry_ids)}"
                if not content:
                    return "UPDATE without content"
            case "MERGE":
                if len(self.entry_ids) < 2:
                    return f"MERGE must target at least two entries, got {len(self.entry_ids)}"
                if not content:
                    return "MERGE without content"
            case "CONFIRM":
                if len(self.entry_ids) != 1:
                    return f"CONFIRM must target exactly one entry, got {len(self.entry_ids)}"
                return None
            case "DISCARD":
                if self.entry_ids:
                    return "DISCARD must not target existing entries"
                if not (self.reason or "").strip():
                    return "DISCARD without a reason"
                return None
            case unhandled:
                # Unreachable while every literal has an arm; a missing one would otherwise abort the
                # whole round at ``_write``'s ``raise`` instead of rejecting the single operation.
                return f"unhandled operation {unhandled}"
        # Reached only by the three ops that persist content: ``_write`` never reads it for CONFIRM
        # or DISCARD, so rejecting those would re-queue an observation over a discarded field.
        return _content_error(content)


class MemoryOperations(BaseModel):
    """Structured output for the consolidation pass."""

    operations: list[MemoryOperation] = Field(
        default_factory=list,
        description=(
            "The operations to apply to this repository's memory. Every observation should be covered "
            f"by one. At most {MAX_OPERATIONS}; anything beyond that is deferred to the next round."
        ),
    )
