"""The swappable classification *method* for Story 1.3's post-run classifier.

This module proposes a **draft** classification of a finished scheduled run's prose
report; it never persists anything and never enforces the load-bearing invariants
(``report`` -> no findings, ``found-issues`` -> non-empty, ``failed`` -> not a finding).
Those are enforced deterministically in :func:`sessions.tasks.classify_run_task` so that
no future method choice (model swap, prompt tuning, or a heuristic) can violate them.

Deliberately standalone: it must **not** import from ``memory`` — the structured-output
helper is a small local copy (the established house convention), so the two concerns can
evolve independently.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from automation.agent.base import BaseAgent

if TYPE_CHECKING:
    from collections.abc import Sequence

# Module-constant prompt (tunable / deferred). Instructs the model to classify a run's prose
# report into one of the three non-failed statuses and emit one actionable per finding. The
# model never authors ``failed`` (decided deterministically by the task before any LLM call),
# nor the ``id`` / ``schema_version`` of an item (the task stamps those via ``build_actionable_item``).
SYSTEM_PROMPT = """You classify the prose report of a finished automated code-agent run so a review console can \
render it at a glance. Read the report and decide a single overall status:

- "all-clear": the run completed and found nothing that needs a human — no issues, no follow-up.
- "found-issues": the run surfaced one or more concrete problems a human should act on. Emit exactly one \
actionable item per distinct problem.
- "needs-attention": the run produced something a human should review, but it is not a discrete, actionable \
finding (e.g. an ambiguous result, a partial outcome, or a summary worth a look).

For each finding under "found-issues", emit one actionable item:
- kind: a short machine-friendly category for the finding (e.g. "bug", "vulnerability", "test-failure", "todo").
- label: a concise human-readable one-line description of the finding.
- ref: what to act on — a file path, symbol, URL, or identifier the reader can locate.
- fix_prompt: OPTIONAL. When the finding can seed an automated fix, a self-contained instruction describing \
the fix; otherwise omit it.

Rules:
- Choose exactly one status.
- If and only if the status is "found-issues", the actionable list must be non-empty; otherwise it must be empty.
- Write a single-line summary (one sentence) that reads well on its own.
- Do not invent problems. Most healthy runs are "all-clear" with no actionable items."""


class ActionableDraft(BaseModel):
    """One proposed actionable item authored by the classifier.

    Carries only the classifier-authored fields; the ``id`` and ``schema_version`` are stamped
    downstream by :func:`sessions.envelopes.build_actionable_item`, never by the model.
    """

    kind: str = Field(description="Short machine-friendly category for the finding (e.g. 'bug', 'test-failure').")
    label: str = Field(description="Concise human-readable one-line description of the finding.")
    ref: str = Field(description="What to act on: a file path, symbol, URL, or identifier.")
    fix_prompt: str | None = Field(
        default=None,
        description="Optional self-contained instruction to seed an automated fix; omit when not applicable.",
    )


class RunClassification(BaseModel):
    """Structured output of the classification pass over a run's prose report."""

    status: Literal["all-clear", "found-issues", "needs-attention"] = Field(
        description="The overall classification of the run. The model never emits 'failed' (decided by the task)."
    )
    summary: str = Field(description="A single-line, self-contained summary of the run's outcome.")
    actionable: list[ActionableDraft] = Field(
        default_factory=list,
        description="One item per finding. Non-empty only when status is 'found-issues'; empty otherwise.",
    )


def _build_structured_llm(schema: type, model_names: Sequence[str]):
    """Structured-output chain with retry + model fallbacks (local copy of the titling/memory pattern).

    No ``max_tokens`` cap: reasoning models count reasoning tokens toward the budget,
    so a tight cap starves the structured-output JSON.
    """

    def _structured(model_name: str):
        return BaseAgent.get_model(model=model_name).with_structured_output(schema).with_retry(stop_after_attempt=2)

    chain = _structured(model_names[0])
    if fallbacks := [_structured(name) for name in model_names[1:]]:
        chain = chain.with_fallbacks(fallbacks)
    return chain


async def classify_response_text(text: str, *, intent, model_names: Sequence[str]) -> RunClassification:
    """Classify a run's prose report into a draft :class:`RunClassification`.

    ``intent`` is carried into the trace metadata only; the report/found-issues/failed
    invariants are enforced by the caller (:func:`sessions.tasks.classify_run_task`), never
    here. ``model_names`` (primary + optional fallbacks) is supplied by the caller from the
    ``run_classifier_*`` site settings.
    """
    structured_llm = _build_structured_llm(RunClassification, model_names)
    return cast(
        "RunClassification",
        await structured_llm.with_config(
            run_name="RunClassification", tags=["RunClassification"], metadata={"intent": str(intent)}
        ).ainvoke([SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=text)]),
    )
