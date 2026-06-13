from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

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


class ConsolidatedMemory(BaseModel):
    """Structured output for the consolidation pass."""

    content: str = Field(description="The full rewritten memory document in markdown. No preamble or commentary.")
