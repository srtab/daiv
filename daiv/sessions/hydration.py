from __future__ import annotations

from typing import Any, NamedTuple

from chat.repo_state import mr_to_payload
from core.checkpointer import aresolve_thread_messages, open_checkpointer


class HydratedThread(NamedTuple):
    """Result of :func:`ahydrate_thread`.

    ``expired`` is True when no checkpoint tuple was found (checkpointer TTL
    expiry, or a thread that never checkpointed); in that case ``messages`` is
    ``[]`` and both payloads are ``None``. Callers branch on ``expired`` to render
    the "expired" notice.
    """

    messages: list[Any]
    expired: bool
    merge_request_payload: dict | None
    diff_stats: dict | None


async def ahydrate_thread(thread_id: str) -> HydratedThread:
    """Return the hydrated messages, expiry flag, MR payload and diff stats for a thread."""
    config = {"configurable": {"thread_id": thread_id}}
    async with open_checkpointer() as cp:
        tup = await cp.aget_tuple(config)
        if tup is None:
            return HydratedThread([], True, None, None)
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
    )
