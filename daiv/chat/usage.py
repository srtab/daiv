from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from automation.agent.base import BaseAgent

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger("daiv.chat")


def get_context_window(model_name: str | None) -> int | None:
    """Return the model's ``max_input_tokens`` via LangChain's profile registry, or
    ``None`` when the model is unknown.

    LangChain pulls profile data from models.dev. Native provider model strings resolve
    cleanly; OpenRouter-prefixed names (e.g. ``openrouter/anthropic/...``) return
    ``None`` — the UI omits the context-window indicator in that case rather than
    guessing.
    """
    if not model_name:
        return None
    try:
        profile = BaseAgent.get_model(model=model_name).profile
    except Exception:  # noqa: BLE001 — third-party lookups can fail in many ways; we want a None fallback.
        logger.debug("get_context_window: profile lookup failed for %r", model_name, exc_info=True)
        return None
    if not profile:
        return None
    window = profile.get("max_input_tokens")
    return int(window) if window else None


def cache_token_totals(by_model: Mapping[str, Mapping[str, Any]]) -> tuple[int, int]:
    """Return ``(cache_read_tokens, cache_write_tokens)`` summed across ``by_model``.

    Mirrors ``_calc_model_cost``'s cache-token derivation in
    ``automation/agent/usage_tracking.py``: ``cache_creation + ephemeral_5m + ephemeral_1h``
    is the cache-write total per model.
    """
    read = 0
    write = 0
    for entry in by_model.values():
        details = (entry or {}).get("input_token_details") or {}
        read += int(details.get("cache_read") or 0)
        write += (
            int(details.get("cache_creation") or 0)
            + int(details.get("ephemeral_5m_input_tokens") or 0)
            + int(details.get("ephemeral_1h_input_tokens") or 0)
        )
    return read, write
