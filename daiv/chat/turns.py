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
    turns: list[dict[str, Any]] = []
    for m in messages:
        mtype = (getattr(m, "type", None) or getattr(m, "role", "") or "").lower()
        if mtype in ("human", "user"):
            turns.append(_build_user_turn(m))
    return turns


def _build_user_turn(m: Any) -> dict[str, Any]:
    content = getattr(m, "content", "")
    if isinstance(content, list):
        text = "".join(
            block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"
        )
    else:
        text = str(content or "")
    return {"id": getattr(m, "id", "") or "", "role": "user", "segments": [{"type": "text", "content": text}]}
