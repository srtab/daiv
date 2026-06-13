from __future__ import annotations

from typing import Any

from automation.agent.utils import extract_text_content

MAX_TOOL_OUTPUT_CHARS = 1_000
MAX_TOOL_ARG_CHARS = 200
MAX_TRANSCRIPT_CHARS = 60_000


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [truncated {len(text) - limit} chars]"


def serialize_transcript(messages: list[Any], *, max_chars: int = MAX_TRANSCRIPT_CHARS) -> str:
    """Serialize a LangGraph message list into a compact plain-text transcript.

    Keeps roles, text content, tool names and truncated args/outputs. When the
    result exceeds ``max_chars``, the middle is elided: the head holds the task
    definition and the tail holds outcomes/corrections — where extraction signal
    lives.
    """
    lines: list[str] = []
    for message in messages:
        msg_type = getattr(message, "type", "unknown")
        if msg_type == "tool":
            name = getattr(message, "name", None) or "tool"
            content = _truncate(extract_text_content(message.content), MAX_TOOL_OUTPUT_CHARS)
            lines.append(f"[tool:{name}] {content}")
            continue
        if text := extract_text_content(message.content):
            lines.append(f"[{msg_type}] {text}")
        for tool_call in getattr(message, "tool_calls", None) or []:
            args = _truncate(str(tool_call.get("args", {})), MAX_TOOL_ARG_CHARS)
            lines.append(f"[{msg_type}:tool_call] {tool_call.get('name', '?')}({args})")

    transcript = "\n".join(lines)
    if len(transcript) <= max_chars:
        return transcript
    head_chars = max_chars // 3
    return transcript[:head_chars] + "\n... [transcript middle elided] ...\n" + transcript[-(max_chars - head_chars) :]
