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
_THINKING_BLOCK_TYPES = frozenset({"thinking", "reasoning"})
_SKILL_TOOL_NAME = "skill"


def build_turns(messages: list[Any]) -> list[dict[str, Any]]:
    """Walk a LangChain message list once, producing a list of turns.

    Each turn is ``{"id", "role", "segments": [...]}``. Segments are either
    ``{"type": "text", "content"}`` or ``{"type": "tool_call", "id", "name",
    "args", "result", "status"}``. Tool results from subsequent ``ToolMessage``
    entries are paired back onto their originating tool-call segment via the
    ``tool_call_id``.

    The ``skill`` tool injects a synthetic ``HumanMessage`` carrying the
    resolved SKILL.md body right after its ``ToolMessage``. On reload we fold
    that body into the skill call's ``result`` instead of rendering it as a
    user turn — it's agent scaffolding, not something the human typed.
    """
    turns: list[dict[str, Any]] = []
    tool_index: dict[str, tuple[int, int]] = {}
    skill_tool_ids: set[str] = set()
    pending_skill_tc_id: str | None = None

    for m in messages:
        mtype = (getattr(m, "type", None) or getattr(m, "role", "") or "").lower()
        if mtype in ("human", "user"):
            if pending_skill_tc_id is not None:
                _fold_skill_body(m, turns, tool_index, pending_skill_tc_id)
                pending_skill_tc_id = None
                continue
            turns.append(_build_user_turn(m))
        elif mtype in ("ai", "assistant"):
            turn = _build_assistant_turn(m)
            turn_idx = len(turns)
            for seg_idx, seg in enumerate(turn["segments"]):
                if seg["type"] == "tool_call" and seg["id"]:
                    tool_index[seg["id"]] = (turn_idx, seg_idx)
                    if seg["name"] == _SKILL_TOOL_NAME:
                        skill_tool_ids.add(seg["id"])
            turns.append(turn)
            pending_skill_tc_id = None
        elif mtype in ("tool", "tool_result"):
            tc_id = _attach_tool_result(m, turns, tool_index)
            pending_skill_tc_id = tc_id if tc_id in skill_tool_ids else None

    return turns


def _attach_tool_result(m: Any, turns: list[dict[str, Any]], tool_index: dict[str, tuple[int, int]]) -> str | None:
    tc_id = getattr(m, "tool_call_id", None) or ((getattr(m, "additional_kwargs", None) or {}).get("tool_call_id"))
    if not tc_id or tc_id not in tool_index:
        logger.warning("chat: dropping orphan ToolMessage with tool_call_id=%r", tc_id)
        return None

    t_idx, s_idx = tool_index[tc_id]
    content = getattr(m, "content", "")
    if isinstance(content, list):
        content = "\n".join(block.get("text", "") if isinstance(block, dict) else str(block) for block in content)
    turns[t_idx]["segments"][s_idx]["result"] = str(content or "")
    return tc_id


def _fold_skill_body(m: Any, turns: list[dict[str, Any]], tool_index: dict[str, tuple[int, int]], tc_id: str) -> None:
    """Replace the skill tool's placeholder result with the injected SKILL.md body."""
    t_idx, s_idx = tool_index[tc_id]
    content = getattr(m, "content", "")
    if isinstance(content, list):
        content = "".join(
            block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text"
        )
    turns[t_idx]["segments"][s_idx]["result"] = str(content or "")


def _reasoning_from_additional_kwargs(additional_kwargs: Any) -> str:
    """Extract reasoning text from an AIMessage's ``additional_kwargs``.

    Mirrors the shapes ``ag_ui_langgraph.resolve_reasoning_content`` recognizes:
    OpenAI legacy ``reasoning.summary[*].text`` and DeepSeek/Qwen/xAI
    ``reasoning_content`` (string).
    """
    if not isinstance(additional_kwargs, dict):
        return ""
    rc = additional_kwargs.get("reasoning_content")
    if isinstance(rc, str) and rc.strip():
        return rc
    reasoning = additional_kwargs.get("reasoning")
    if isinstance(reasoning, dict):
        summary = reasoning.get("summary") or []
        if isinstance(summary, list):
            joined = "\n\n".join(
                str(item.get("text", "")) for item in summary if isinstance(item, dict) and item.get("text")
            )
            if joined.strip():
                return joined
    return ""


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

    # Some providers (DeepSeek/Qwen/xAI, certain OpenRouter routes) return
    # reasoning out-of-band in ``additional_kwargs`` instead of as a content
    # block. Surface it as a leading thinking segment so refresh matches the
    # streamed view.
    extra_thought = _reasoning_from_additional_kwargs(getattr(m, "additional_kwargs", None))
    if extra_thought:
        segments.append({"type": "thinking", "content": extra_thought})

    if isinstance(content, list):
        for block in content:
            btype = block.get("type") if isinstance(block, dict) else getattr(block, "type", None)
            if btype == "text":
                text = block.get("text") if isinstance(block, dict) else getattr(block, "text", "")
                if text:
                    segments.append({"type": "text", "content": text})
            elif btype in _THINKING_BLOCK_TYPES:
                # Anthropic uses ``thinking``; the newer LangChain standard uses ``reasoning``.
                thought = (
                    (block.get("thinking") or block.get("reasoning") or block.get("text") or "")
                    if isinstance(block, dict)
                    else ""
                )
                if thought:
                    segments.append({"type": "thinking", "content": thought})
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
