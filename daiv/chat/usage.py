from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from automation.agent.base import BaseAgent

if TYPE_CHECKING:
    from collections.abc import Mapping

    from chat.models import ChatThread

logger = logging.getLogger("daiv.chat")


# Wire identifier — the JS receiver in chat-stream.js compares against this literal.
USAGE_SUMMARY_EVENT_NAME = "daiv.usage_summary"


def build_thread_usage_payload(thread: ChatThread | None) -> dict[str, Any]:
    """Single source of truth for the chat ``usage_summary`` wire shape."""
    if thread is None:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cost_usd": None,
            "by_model": {},
            "last_model_name": None,
            "last_input_tokens": 0,
            "context_window": None,
        }
    return {
        "input_tokens": thread.input_tokens or 0,
        "output_tokens": thread.output_tokens or 0,
        "total_tokens": thread.total_tokens or 0,
        "cache_read_tokens": thread.cache_read_tokens,
        "cache_write_tokens": thread.cache_write_tokens,
        "cost_usd": (str(thread.cost_usd) if thread.cost_priced and thread.cost_usd is not None else None),
        "by_model": thread.usage_by_model or {},
        "last_model_name": thread.last_model_name or None,
        "last_input_tokens": thread.last_input_tokens,
        "context_window": get_context_window(thread.last_model_name or None),
    }


# Cache only successful integer resolutions. Caching ``None`` would turn a
# transient registry/network blip into a permanent UI degradation for the rest
# of the worker's lifetime, so failed lookups stay un-cached and retry on
# the next call.
_CONTEXT_WINDOW_CACHE: dict[str, int] = {}
_CONTEXT_WINDOW_CACHE_MAX = 128


def _lookup_max_input_tokens(model_name: str) -> int | None:
    try:
        profile = BaseAgent.get_model(model=model_name).profile
    except Exception:  # noqa: BLE001 — third-party lookups can fail in many ways.
        logger.warning("get_context_window: profile lookup failed for %r", model_name, exc_info=True)
        return None
    if not profile:
        return None
    window = profile.get("max_input_tokens")
    return int(window) if window else None


def get_context_window(model_name: str | None) -> int | None:
    """Return the model's ``max_input_tokens``, or ``None`` when unknown.

    OpenRouter-prefixed names (``openrouter/anthropic/...``) and unrecognised
    models return ``None`` — the UI omits the context-window indicator in that
    case rather than guessing.
    """
    if not model_name:
        return None
    cached = _CONTEXT_WINDOW_CACHE.get(model_name)
    if cached is not None:
        return cached
    window = _lookup_max_input_tokens(model_name)
    if window is not None:
        if len(_CONTEXT_WINDOW_CACHE) >= _CONTEXT_WINDOW_CACHE_MAX:
            _CONTEXT_WINDOW_CACHE.pop(next(iter(_CONTEXT_WINDOW_CACHE)))
        _CONTEXT_WINDOW_CACHE[model_name] = window
    return window


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
