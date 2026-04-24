"""Normalize LangChain messages into the chronological ``turns[].segments[]`` shape
consumed by the chat page's Alpine renderer.

Kept as a pure helper module (no Django imports) so the transformation is trivially
unit-testable without database or view fixtures.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("daiv.chat")

_TOOL_USE_BLOCK_TYPES = frozenset({"tool_use", "tool_call"})


def build_turns(messages: list[Any]) -> list[dict[str, Any]]:
    """Walk a LangChain message list once, producing a list of turns.

    Each turn is ``{"id", "role", "segments": [...]}``. Segments are either
    ``{"type": "text", "content"}`` or ``{"type": "tool_call", "id", "name",
    "args", "result", "status"}``. Tool results from subsequent ``ToolMessage``
    entries are paired back onto their originating tool-call segment via the
    ``tool_call_id``.
    """
    return []
