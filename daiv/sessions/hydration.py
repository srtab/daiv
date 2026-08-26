from __future__ import annotations

import logging
from typing import Any, NamedTuple

from automation.agent.events import context_usage_payload
from automation.agent.usage_tracking import message_model_name, resolve_window_by_name
from chat.repo_state import mr_to_payload
from chat.turns import is_assistant_message
from core.checkpointer import aresolve_thread_messages, open_checkpointer

logger = logging.getLogger("daiv.sessions")


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
    (genai-prices) alone — the one case where a reload can shift the ring.
    """
    for message in reversed(messages):
        if not is_assistant_message(message):
            continue
        usage = getattr(message, "usage_metadata", None)
        model_name = message_model_name(message)
        if not usage or not model_name:
            continue
        return context_usage_payload(model=model_name, usage=usage, window=resolve_window_by_name(model_name))
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
    try:
        context_usage = derive_context_usage(messages)
    except Exception:
        # A checkpoint shape the walk can't parse is a real bug, but it must cost the
        # meter its seed, never the page render or the transcript poller.
        logger.exception("Context-usage seed derivation failed for thread %s", thread_id)
        context_usage = None
    return HydratedThread(
        messages,
        False,
        mr_to_payload(channel_values.get("merge_request")),
        diff_stats if isinstance(diff_stats, dict) else None,
        context_usage,
    )
