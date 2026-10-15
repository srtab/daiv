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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

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
