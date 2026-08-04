from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


def build_structured_llm(schema: type, model_names: Sequence[str]):
    """Structured-output chain with retry + model fallbacks (same pattern as titling).

    No ``max_tokens`` cap: reasoning models count reasoning tokens toward the budget,
    so a tight cap starves the structured-output JSON.
    """
    from automation.agent.base import BaseAgent

    def _structured(model_name: str):
        return BaseAgent.get_model(model=model_name).with_structured_output(schema).with_retry(stop_after_attempt=2)

    chain = _structured(model_names[0])
    if fallbacks := [_structured(name) for name in model_names[1:]]:
        chain = chain.with_fallbacks(fallbacks)
    return chain
