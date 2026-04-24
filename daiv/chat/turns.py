"""Normalize LangChain messages into the chronological ``turns[].segments[]`` shape
consumed by the chat page's Alpine renderer.

Kept as a pure helper module (no Django imports) so the transformation is trivially
unit-testable without database or view fixtures.
"""

from __future__ import annotations

import json
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
        elif mtype in ("ai", "assistant"):
            turns.append(_build_assistant_turn(m))
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


def _build_assistant_turn(m: Any) -> dict[str, Any]:
    content = getattr(m, "content", "")
    tool_calls = getattr(m, "tool_calls", None) or []
    tc_by_id: dict[str, Any] = {}
    for tc in tool_calls:
        tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
        if tc_id:
            tc_by_id[tc_id] = tc

    segments: list[dict[str, Any]] = []

    if isinstance(content, list):
        for block in content:
            btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            if btype == "text":
                text = block.get("text") if isinstance(block, dict) else getattr(block, "text", "")
                if text:
                    segments.append({"type": "text", "content": text})
            elif btype in _TOOL_USE_BLOCK_TYPES:
                tc_id = block.get("id") if isinstance(block, dict) else getattr(block, "id", None)
                canonical = tc_by_id.get(tc_id, block)
                segments.append(_tool_call_segment(canonical))
    else:
        if isinstance(content, str) and content.strip():
            segments.append({"type": "text", "content": content})
        for tc in tool_calls:
            segments.append(_tool_call_segment(tc))

    return {"id": getattr(m, "id", "") or "", "role": "assistant", "segments": segments}


def _tool_call_segment(tc: Any, *, status: str = "done") -> dict[str, Any]:
    if isinstance(tc, dict):
        tc_id = tc.get("id") or ""
        tc_name = tc.get("name") or ""
        args = tc.get("args", tc.get("input", tc.get("arguments", "")))
    else:
        tc_id = getattr(tc, "id", "") or ""
        tc_name = getattr(tc, "name", "") or ""
        args = getattr(tc, "args", None) or getattr(tc, "input", None) or ""

    args_str = json.dumps(args) if isinstance(args, (dict, list)) else str(args or "")

    return {"type": "tool_call", "id": tc_id, "name": tc_name, "args": args_str, "result": None, "status": status}
