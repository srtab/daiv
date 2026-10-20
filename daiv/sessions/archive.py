"""The session archive: the durable copy of a session's agent state.

The Redis checkpoint is the working copy and expires after ``DJANGO_REDIS_CHECKPOINT_TTL_MINUTES``. When a run ends,
``aarchive_thread`` copies the thread's :data:`ARCHIVED_CHANNELS` into a :class:`~sessions.models.SessionArchive` row,
one per context. ``sessions.hydration`` shows the archived contexts before the live one, and the latest in its place
once the checkpoint is gone; ``arestore_thread`` seeds an empty checkpoint from the latest context before a run, so an
expired session stays readable and resumes.

A run that clears its thread (``/clear``) closes the latest context: the next state archived opens the next epoch, and
a closed context is never written to or restored again. Any other run merges its messages into the latest context
(:func:`merge_messages`), so a thread whose oldest writes expired still extends it, and a context never shrinks.
"""

from __future__ import annotations

import asyncio
import logging
import zlib
from typing import TYPE_CHECKING, Any, NamedTuple

from django.utils import timezone

from automation.agent.git_state import checkpointed_merge_request
from sessions.models import Session, SessionArchive

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import BaseCheckpointSaver
    from langgraph.pregel import Pregel

logger = logging.getLogger("daiv.sessions")

# Never session_id (its container is reaped) or memory_contents/skills_metadata (AGENTS.md and the skills would stop
# reloading); a channel left out resets on restore.
ARCHIVED_CHANNELS = ("messages", "merge_request", "diff_stats", "todos", "loaded_tool_names", "_summarization_event")


class ArchiveRestoreError(Exception):
    """A session's archived context exists but could not be restored into its thread."""


def archivable_values(values: Mapping[str, Any]) -> dict[str, Any]:
    """The archived channels of a thread's state that hold a value."""
    return {key: values[key] for key in ARCHIVED_CHANNELS if values.get(key) is not None}


def archive_row(values: Mapping[str, Any]) -> dict[str, Any]:
    """The ``SessionArchive`` fields that hold ``values``: the checkpoint serializer's output, compressed, and the
    count and first id of its messages."""
    from core.checkpointer import DAIVRedisSerializer

    messages = values.get("messages") or []
    if not messages or not messages[0].id:
        raise ValueError("An archived context needs messages, the first of them with an id")
    state_type, data = DAIVRedisSerializer().dumps_typed(dict(values))
    return {
        "state": zlib.compress(data),
        "state_type": state_type,
        "first_message_id": messages[0].id,
        "message_count": len(messages),
    }


def decode_values(state_type: str, state: bytes) -> dict[str, Any]:
    """The archived channels a ``SessionArchive`` row's ``state`` holds."""
    from core.checkpointer import DAIVRedisSerializer

    return archivable_values(DAIVRedisSerializer().loads_typed((state_type, zlib.decompress(state))))


class ArchivedContext(NamedTuple):
    pk: int
    epoch: int
    first_message_id: str
    message_count: int
    closed_at: datetime | None

    @property
    def closed(self) -> bool:
        return self.closed_at is not None


async def aarchived_contexts(thread_id: str) -> list[ArchivedContext]:
    """The session's archived contexts, oldest first, without their state."""
    rows = SessionArchive.objects.filter(session_id=thread_id).order_by("epoch")
    return [ArchivedContext(*row) async for row in rows.values_list(*ArchivedContext._fields)]


async def aload_archived_values(pk: int) -> dict[str, Any]:
    state_type, state = await SessionArchive.objects.filter(pk=pk).values_list("state_type", "state").aget()
    return await asyncio.to_thread(decode_values, state_type, state)


def continues_context(first_message_id: str, messages: Sequence[Any]) -> bool:
    """Whether ``messages`` still start with the message that opened the context."""
    return bool(messages) and messages[0].id == first_message_id


def merge_messages(archived: Sequence[Any], live: Sequence[Any]) -> list[Any]:
    """``live``, after the archived messages it no longer holds: those before its first message, or all of them when
    that message is new.

    A thread whose oldest writes expired starts inside its context and resends a first message it lost at the end;
    that copy is dropped, so the message keeps its place.
    """
    ids = [message.id for message in archived]
    start = ids.index(live[0].id) if live and live[0].id in ids else len(ids)
    head_ids = set(ids[:start])
    return [*archived[:start], *(message for message in live if message.id not in head_ids)]


async def amerge_into_context(context: ArchivedContext, messages: list[Any]) -> list[Any]:
    """``messages`` merged into ``context`` (:func:`merge_messages`) unless they still start it."""
    if continues_context(context.first_message_id, messages):
        return messages
    archived = (await aload_archived_values(context.pk)).get("messages") or []
    return merge_messages(archived, messages)


async def aarchive_thread(checkpointer: BaseCheckpointSaver, thread_id: str, *, reset: bool = False) -> bool:
    """Copy the thread's latest state into its session's archive; ``True`` when a row was written.

    ``reset`` marks a run that cleared its thread (``/clear``): once the thread no longer holds the latest context's
    first message, that context is closed. A context that would shrink is kept, and an error logged.
    """
    from core.checkpointer import aread_thread_values

    values = archivable_values(await aread_thread_values(checkpointer, thread_id) or {})
    messages = values.get("messages") or []
    contexts = await aarchived_contexts(thread_id)
    latest = contexts[-1] if contexts else None
    if reset and latest is not None and not latest.closed and all(m.id != latest.first_message_id for m in messages):
        latest = await _aclose(latest)
    if not messages:
        return False
    if latest is None:
        if not await Session.objects.filter(pk=thread_id).aexists():
            logger.warning("No session %s to archive its thread under", thread_id)
            return False
        epoch = 0
    elif latest.closed:
        epoch = latest.epoch + 1
    else:
        epoch = latest.epoch
        messages = values["messages"] = await amerge_into_context(latest, messages)
        if len(messages) < latest.message_count:
            logger.error(
                "Thread %s holds %d messages, fewer than its archived %d; keeping the archive",
                thread_id,
                len(messages),
                latest.message_count,
            )
            return False
    row = await asyncio.to_thread(archive_row, values)
    await SessionArchive.objects.defer("state").aupdate_or_create(session_id=thread_id, epoch=epoch, defaults=row)
    return True


async def _aclose(context: ArchivedContext) -> ArchivedContext:
    closed_at = timezone.now()
    await SessionArchive.objects.filter(pk=context.pk).aupdate(closed_at=closed_at)
    return context._replace(closed_at=closed_at)


async def arestore_thread(agent: Pregel, checkpointer: BaseCheckpointSaver, config: RunnableConfig) -> bool:
    """Seed the thread's empty checkpoint from its latest archived context; ``True`` when it did.

    A thread with a checkpoint, without an archive or whose latest context is closed is left alone. A context that
    fails to restore raises :class:`ArchiveRestoreError` rather than let the run start without it.
    """
    thread_id = config["configurable"]["thread_id"]
    if await checkpointer.aget_tuple(config) is not None:
        return False
    contexts = await aarchived_contexts(thread_id)
    if not contexts or contexts[-1].closed:
        return False
    latest = contexts[-1]
    try:
        values = await aload_archived_values(latest.pk)
        if checkpointed_merge_request(values, strict=False) is None:
            values.pop("merge_request", None)
        # The model node writes every channel, private ones included; the input node would silently drop
        # loaded_tool_names and _summarization_event.
        await agent.aupdate_state(config, values, as_node="model")
    except Exception as exc:
        raise ArchiveRestoreError(
            f"Could not restore this session's conversation from its archive (context {latest.epoch})"
        ) from exc
    logger.info("Restored thread %s from archived context %d", thread_id, latest.epoch)
    return True
