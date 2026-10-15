from __future__ import annotations

import logging
from typing import Any, NamedTuple

from automation.agent.events import context_usage_payload
from automation.agent.usage_tracking import message_model_name, resolve_window_by_name
from core.checkpointer import aread_thread_values, open_checkpointer
from sessions.archive import aarchived_contexts, aload_archived_values, continues_context
from sessions.repo_state import mr_to_payload
from sessions.turns import is_assistant_message

logger = logging.getLogger("daiv.sessions")


class HydratedThread(NamedTuple):
    """Result of :func:`ahydrate_thread`.

    ``expired`` is True when neither a checkpoint nor an archived context was
    found; in that case ``messages`` is ``[]`` and the other payloads are ``None``.
    Callers branch on ``expired`` to render the "expired" notice.
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
    seed for a thread.

    The live checkpoint is the current context; once it expires, the latest archived context stands in. Archived
    contexts from before a ``/clear`` come first, so the transcript keeps them; the payloads and the meter come from
    the current context alone.
    """
    async with open_checkpointer() as cp:
        current = await aread_thread_values(cp, thread_id)
    contexts = await aarchived_contexts(thread_id)
    if current is None and contexts:
        current = await aload_archived_values(contexts.pop().pk)
    elif current is not None and contexts and continues_context(contexts[-1].first_message_id, current["messages"]):
        contexts.pop()
    if current is None:
        return HydratedThread([], True, None, None, None)
    earlier: list[Any] = []
    for context in contexts:
        earlier.extend((await aload_archived_values(context.pk)).get("messages") or [])
    messages = current.get("messages") or []
    diff_stats = current.get("diff_stats")
    try:
        context_usage = derive_context_usage(messages)
    except Exception:
        # A checkpoint shape the walk can't parse is a real bug, but it must cost the
        # meter its seed, never the page render or the transcript poller.
        logger.exception("Context-usage seed derivation failed for thread %s", thread_id)
        context_usage = None
    return HydratedThread(
        earlier + messages,
        False,
        mr_to_payload(current.get("merge_request")),
        diff_stats if isinstance(diff_stats, dict) else None,
        context_usage,
    )
