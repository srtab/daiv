from __future__ import annotations

from typing import Any

from chat.repo_state import mr_to_payload
from core.checkpointer import open_checkpointer


async def ahydrate_thread(thread_id: str) -> tuple[list[Any], bool, dict | None]:
    """Return (messages, expired, merge_request_payload) for a thread."""
    async with open_checkpointer() as cp:
        tup = await cp.aget_tuple({"configurable": {"thread_id": thread_id}})
    if tup is None:
        return [], True, None
    channel_values = (tup.checkpoint or {}).get("channel_values", {})
    messages = channel_values.get("messages", [])
    return messages, False, mr_to_payload(channel_values.get("merge_request"))
