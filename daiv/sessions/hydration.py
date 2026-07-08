from __future__ import annotations

from typing import Any, NamedTuple

from chat.repo_state import mr_to_payload
from core.checkpointer import open_checkpointer


class HydratedThread(NamedTuple):
    """Result of :func:`ahydrate_thread`.

    ``expired`` is True when no checkpoint tuple was found (checkpointer TTL
    expiry, or a thread that never checkpointed); in that case ``messages`` is
    ``[]`` and ``merge_request_payload`` is ``None``. Callers branch on
    ``expired`` to render the "expired" notice.
    """

    messages: list[Any]
    expired: bool
    merge_request_payload: dict | None


async def ahydrate_thread(thread_id: str) -> HydratedThread:
    """Return the hydrated messages, expiry flag, and MR payload for a thread."""
    async with open_checkpointer() as cp:
        tup = await cp.aget_tuple({"configurable": {"thread_id": thread_id}})
    if tup is None:
        return HydratedThread([], True, None)
    channel_values = (tup.checkpoint or {}).get("channel_values", {})
    messages = channel_values.get("messages", [])
    return HydratedThread(messages, False, mr_to_payload(channel_values.get("merge_request")))
