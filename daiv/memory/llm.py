from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence


def build_structured_llm(schema: type, model_names: Sequence[str]):
    """Structured-output chain with model fallbacks.

    No ``max_tokens`` cap: reasoning models count reasoning tokens toward the budget,
    so a tight cap starves the structured-output JSON.

    Deliberately no ``with_retry``: the dominant failure is the model returning no tool call at all,
    which surfaces as a ``None`` result rather than an exception, so a retry layer never fired on it.
    The fallback *model* is the only escape that can change the outcome — see AGENTS.md
    §"Repository memory". ``sessions.classification`` keeps its retry on purpose.
    """
    from automation.agent.base import BaseAgent

    def _structured(model_name: str):
        return BaseAgent.get_model(model=model_name).with_structured_output(schema)

    chain = _structured(model_names[0])
    if fallbacks := [_structured(name) for name in model_names[1:]]:
        chain = chain.with_fallbacks(fallbacks)
    return chain
