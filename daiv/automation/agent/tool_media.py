"""What the tool-media middleware needs from wherever the media of tool results is kept.

A tool result can carry media blocks (an MCP screenshot, the image or PDF ``read_file`` opened). The run layer hands
``create_daiv_agent`` a :class:`ToolMediaStore` (``sessions.tool_media.RunToolMediaStore`` for executor runs) so the
chat can show them; without one, nothing is kept. Standard library only: ``sessions`` imports this module.
"""

from __future__ import annotations

from typing import Any, Protocol

DAIV_MEDIA_KEY = "daiv_media"
MEDIA_BLOCK_TYPES = frozenset({"image", "file", "audio", "video"})

TOO_LARGE = "too_large"
RUN_LIMIT = "run_limit"
UNSUPPORTED_TYPE = "unsupported_type"
FAILED = "failed"


class ToolMediaError(Exception):
    """A media block the store did not keep; ``code`` is one of the error codes above."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ToolMediaStore(Protocol):
    @property
    def max_bytes(self) -> int:
        """The largest ``content`` :meth:`astore` keeps; bigger blocks are rejected before they are decoded."""
        ...

    async def astore(self, *, thread_id: str, tool_call_id: str, ordinal: int, mime_type: str, content: bytes) -> str:
        """Keep ``content`` and return its id; raise :class:`ToolMediaError` to reject it."""
        ...


def result_messages(result: Any) -> list[Any]:
    """The messages a tool call returned: a ``Command``'s ``update["messages"]``, else ``result`` itself."""
    update = getattr(result, "update", None)
    if isinstance(update, dict):
        return list(update.get("messages") or [])
    return [result]


def media_blocks(message: Any) -> list[dict[str, Any]]:
    """The media blocks of ``message``, in order: its standard ``content_blocks``, else its raw list content."""
    blocks = getattr(message, "content_blocks", None)
    if blocks is None:
        content = getattr(message, "content", None)
        blocks = content if isinstance(content, list) else []
    return [block for block in blocks if isinstance(block, dict) and block.get("type") in MEDIA_BLOCK_TYPES]
