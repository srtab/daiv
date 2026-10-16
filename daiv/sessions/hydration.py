from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, NamedTuple

from automation.agent.events import context_usage_payload
from automation.agent.usage_tracking import message_model_name, resolve_window_by_name
from core.checkpointer import aread_thread_values, open_checkpointer
from sessions.archive import aarchived_contexts, aload_archived_values, continues_context, merge_messages
from sessions.repo_state import mr_to_payload
from sessions.turns import is_assistant_message

if TYPE_CHECKING:
    from sessions.archive import ArchivedContext

logger = logging.getLogger("daiv.sessions")


class HydratedThread(NamedTuple):
    """Result of :func:`ahydrate_thread`.

    ``messages`` can span several contexts; the payloads and ``context_usage`` come from the current one alone.
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

    The live checkpoint is the current context, merged into the latest archived one while that context is open, so
    a checkpoint that lost its oldest writes still shows them; once it expires, the latest archived context stands in.
    Archived contexts before it come first, so the transcript keeps them. A context that fails to load is logged and
    left out.
    """
    from automation.agent.publishers import checkpointed_merge_request

    async with open_checkpointer() as cp:
        current = await aread_thread_values(cp, thread_id)
    contexts = await aarchived_contexts(thread_id)
    if current is not None and contexts and not contexts[-1].closed:
        latest = contexts.pop()
        live = current.get("messages") or []
        if not continues_context(latest.first_message_id, live):
            archived = await _aload_context(thread_id, latest)
            if archived is not None:
                current["messages"] = merge_messages(archived.get("messages") or [], live)
    earlier = [values for context in contexts if (values := await _aload_context(thread_id, context)) is not None]
    if current is None:
        if not earlier:
            return HydratedThread([], True, None, None, None)
        current = earlier.pop()
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
        [message for values in earlier for message in values.get("messages") or []] + messages,
        False,
        mr_to_payload(checkpointed_merge_request(current, strict=False)),
        diff_stats if isinstance(diff_stats, dict) else None,
        context_usage,
    )


async def _aload_context(thread_id: str, context: ArchivedContext) -> dict[str, Any] | None:
    try:
        return await aload_archived_values(context.pk)
    except Exception:
        logger.exception("Failed to load archived context %d of thread %s; leaving it out", context.epoch, thread_id)
        return None
