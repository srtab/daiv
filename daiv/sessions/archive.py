"""The session archive: the durable copy of a session's agent state.

The Redis checkpoint is the working copy and expires after ``DJANGO_REDIS_CHECKPOINT_TTL_MINUTES``. When a run ends,
``aarchive_thread`` copies the thread's :data:`ARCHIVED_CHANNELS` into a :class:`~sessions.models.SessionArchive` row,
one per context. ``sessions.hydration`` reads the archive once the checkpoint is gone, and ``arestore_thread`` seeds an
empty checkpoint from the latest context before a run, so an expired session stays readable and resumes.

A run whose messages no longer hold the latest context's first message reset it (``/clear``): its state opens the next
epoch and the earlier context is kept. A run never overwrites a context with fewer of its messages.
"""

from __future__ import annotations

import logging
import zlib
from typing import TYPE_CHECKING, Any, NamedTuple

from sessions.models import Session, SessionArchive

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from langgraph.checkpoint.base import BaseCheckpointSaver

logger = logging.getLogger("daiv.sessions")

# Not session_id (its container is reaped), memory_contents or skills_metadata (they would stop AGENTS.md and the
# skills reloading), nor the per-run flags.
ARCHIVED_CHANNELS = ("messages", "merge_request", "diff_stats", "todos", "loaded_tool_names", "_summarization_event")


def archivable_values(values: Mapping[str, Any]) -> dict[str, Any]:
    """The archived channels of a thread's state that hold a value."""
    return {key: values[key] for key in ARCHIVED_CHANNELS if values.get(key) is not None}


def encode_values(values: Mapping[str, Any]) -> tuple[str, bytes]:
    """``(state_type, state)`` for a ``SessionArchive`` row: the checkpoint serializer's output, compressed."""
    from core.checkpointer import DAIVRedisSerializer

    state_type, data = DAIVRedisSerializer().dumps_typed(dict(values))
    return state_type, zlib.compress(data)


def decode_values(state_type: str, state: bytes) -> dict[str, Any]:
    """The inverse of :func:`encode_values`."""
    from core.checkpointer import DAIVRedisSerializer

    return DAIVRedisSerializer().loads_typed((state_type, zlib.decompress(state)))


class ArchivedContext(NamedTuple):
    pk: int
    epoch: int
    first_message_id: str
    message_count: int


async def aarchived_contexts(thread_id: str) -> list[ArchivedContext]:
    """The session's archived contexts, oldest first, without their state."""
    rows = SessionArchive.objects.filter(session_id=thread_id).order_by("epoch")
    return [ArchivedContext(*row) async for row in rows.values_list("pk", "epoch", "first_message_id", "message_count")]


async def aload_archived_values(pk: int) -> dict[str, Any]:
    state_type, state = await SessionArchive.objects.filter(pk=pk).values_list("state_type", "state").aget()
    return decode_values(state_type, state)


def continues_context(first_message_id: str, messages: Sequence[Any]) -> bool:
    """Whether ``messages`` still hold the context opened by ``first_message_id``; an unknown id is assumed to."""
    return not first_message_id or any(message.id == first_message_id for message in messages)


async def aarchive_thread(checkpointer: BaseCheckpointSaver, thread_id: str) -> bool:
    """Copy the thread's latest state into its session's archive; ``True`` when a row was written.

    Best-effort: a failure is logged, never raised, so archiving cannot fail a run that already ended.
    """
    from core.checkpointer import aread_thread_values

    try:
        archivable = archivable_values(await aread_thread_values(checkpointer, thread_id) or {})
        messages = archivable.get("messages") or []
        if not messages:
            return False
        if not await Session.objects.filter(pk=thread_id).aexists():
            logger.warning("No session %s to archive its thread under", thread_id)
            return False
        epoch = await _aepoch_for(thread_id, messages)
        if epoch is None:
            return False
        state_type, state = encode_values(archivable)
        await SessionArchive.objects.aupdate_or_create(
            session_id=thread_id,
            epoch=epoch,
            defaults={
                "state": state,
                "state_type": state_type,
                "first_message_id": messages[0].id or "",
                "message_count": len(messages),
            },
        )
    except Exception:
        logger.exception("Failed to archive thread %s", thread_id)
        return False
    return True


async def _aepoch_for(thread_id: str, messages: Sequence[Any]) -> int | None:
    """The epoch ``messages`` belong to, or ``None`` when writing them would shrink an archived context."""
    contexts = await aarchived_contexts(thread_id)
    if not contexts:
        return 0
    latest = contexts[-1]
    if continues_context(latest.first_message_id, messages):
        if len(messages) < latest.message_count:
            logger.warning(
                "Thread %s holds %d messages, fewer than its archived %d; keeping the archive",
                thread_id,
                len(messages),
                latest.message_count,
            )
            return None
        return latest.epoch
    archived_ids = {message.id for message in (await aload_archived_values(latest.pk)).get("messages") or []}
    if messages[0].id in archived_ids:
        logger.warning("Thread %s now starts inside its archived context; keeping the archive", thread_id)
        return None
    return latest.epoch + 1
