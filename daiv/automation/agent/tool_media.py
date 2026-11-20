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


class ToolContent:
    """A message a tool call returned, read for what the transcript shows of it: its media blocks and its text."""

    def __init__(self, message: Any) -> None:
        self.message = message

    @staticmethod
    def messages(result: Any) -> list[Any]:
        """The messages a tool call returned: a ``Command``'s ``update["messages"]``, else ``result`` itself."""
        update = getattr(result, "update", None)
        if isinstance(update, dict):
            return list(update.get("messages") or [])
        return [result]

    @property
    def media_blocks(self) -> list[dict[str, Any]]:
        """The media blocks, in order: the standard ``content_blocks``, else the raw list content."""
        blocks = getattr(self.message, "content_blocks", None)
        if blocks is None:
            content = getattr(self.message, "content", None)
            blocks = content if isinstance(content, list) else []
        return [block for block in blocks if isinstance(block, dict) and block.get("type") in MEDIA_BLOCK_TYPES]

    @property
    def text(self) -> str:
        """The text, one block per line: each dict block's string ``text`` and every other block as a string; a block
        without text, such as an image, adds no line."""
        content = getattr(self.message, "content", "")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return str(content or "")
        texts = (block.get("text") if isinstance(block, dict) else str(block) for block in content)
        return "\n".join(text for text in texts if isinstance(text, str))
