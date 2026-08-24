from __future__ import annotations

from typing import Any, NamedTuple

from automation.agent.events import context_usage_payload
from automation.agent.usage_tracking import genai_prices_window
from chat.repo_state import mr_to_payload
from core.checkpointer import aresolve_thread_messages, open_checkpointer


class HydratedThread(NamedTuple):
    """Result of :func:`ahydrate_thread`.

    ``expired`` is True when no checkpoint tuple was found (checkpointer TTL
    expiry, or a thread that never checkpointed); in that case ``messages`` is
    ``[]`` and the other payloads are ``None``. Callers branch on ``expired`` to
    render the "expired" notice.
    """

    messages: list[Any]
    expired: bool
    merge_request_payload: dict | None
    diff_stats: dict | None
    context_usage: dict | None


def derive_context_usage(messages: list[Any]) -> dict | None:
    """Context-meter seed: the last AI message carrying usage, through the same builder the
    live middleware uses. Only the model *name* is in hand here, so the window is tier 2
    (genai-prices) alone — the one case where a reload can shift the ring (design §2).
    """
    for message in reversed(messages):
        if (getattr(message, "type", "") or "").lower() not in ("ai", "assistant"):
            continue
        usage = getattr(message, "usage_metadata", None)
        model_name = (getattr(message, "response_metadata", None) or {}).get("model_name")
        if not usage or not model_name:
            continue
        window = genai_prices_window(model_name)
        return context_usage_payload(
            model=model_name,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cached_tokens=(usage.get("input_token_details") or {}).get("cache_read", 0),
            window_tokens=window,
            window_source="genai_prices" if window else None,
        )
    return None


async def ahydrate_thread(thread_id: str) -> HydratedThread:
    """Return the hydrated messages, expiry flag, MR payload, diff stats and context-usage
    seed for a thread."""
    config = {"configurable": {"thread_id": thread_id}}
    async with open_checkpointer() as cp:
        tup = await cp.aget_tuple(config)
        if tup is None:
            return HydratedThread([], True, None, None, None)
        channel_values = (tup.checkpoint or {}).get("channel_values", {})
        # ``messages`` lives in a deepagents ``DeltaChannel`` and is usually absent from
        # ``channel_values``; resolve it via the delta-history contract (see the helper).
        messages = await aresolve_thread_messages(cp, config, channel_values)
    diff_stats = channel_values.get("diff_stats")
    return HydratedThread(
        messages,
        False,
        mr_to_payload(channel_values.get("merge_request")),
        diff_stats if isinstance(diff_stats, dict) else None,
        derive_context_usage(messages),
    )
