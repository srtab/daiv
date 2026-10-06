"""Deterministic checks for the todo, web-search and explore coverage cases.

Each check reads what the agent did (its tool calls, its final message), never which words it chose.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from langchain_core.messages import ToolMessage

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from langchain_core.messages import BaseMessage

EDIT_TOOLS = frozenset({"edit_file", "write_file"})

_SOURCES_HEADING = re.compile(r"^\s*(?:#{1,6}\s+)?\**\s*sources\s*:?\s*\**\s*:?\s*$", re.IGNORECASE)
_LINK_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[[^\]]+\]\(https?://[^)\s]+\)")


def todo_planning_violation(tool_calls: Sequence[Mapping]) -> str | None:
    """Why ``tool_calls`` fail "plan with ``write_todos`` before the first edit", or ``None`` when they pass."""
    names = [call["name"] for call in tool_calls]
    if "write_todos" not in names:
        return f"write_todos was never called; tool calls: {names}"
    first_edit = next((index for index, name in enumerate(names) if name in EDIT_TOOLS), None)
    if first_edit is not None and first_edit < names.index("write_todos"):
        return f"{names[first_edit]} ran before write_todos; tool calls: {names}"
    return None


def one_step_violation(tool_calls: Sequence[Mapping]) -> str | None:
    """Why ``tool_calls`` fail "make a one-step edit without ``write_todos``", or ``None`` when they pass."""
    names = [call["name"] for call in tool_calls]
    if "write_todos" in names:
        return f"write_todos was called for a one-step edit; tool calls: {names}"
    if not {*EDIT_TOOLS, "bash"} & set(names):
        return f"the agent never tried the edit; tool calls: {names}"
    return None


def search_backend_failed(messages: Sequence[BaseMessage], tool_name: str) -> bool:
    """Whether ``tool_name`` ran and every result was a backend ``error:``, which is not a prompt outcome."""
    results = [message for message in messages if isinstance(message, ToolMessage) and message.name == tool_name]
    return bool(results) and all(str(message.content).startswith("error:") for message in results)


def trailing_source_links(reply: str) -> list[str]:
    """The linked list items under the reply's last ``Sources:`` heading; ``[]`` unless the reply ends with them."""
    lines = reply.rstrip().splitlines()
    heading = next((index for index in range(len(lines) - 1, -1, -1) if _SOURCES_HEADING.match(lines[index])), None)
    if heading is None:
        return []
    items = [line for line in lines[heading + 1 :] if line.strip()]
    return items if items and all(_LINK_ITEM.match(line) for line in items) else []


def calls_outside(tool_calls: Sequence[Mapping], allowed: Collection[str]) -> list[str]:
    return [call["name"] for call in tool_calls if call["name"] not in allowed]


def absolute_paths(text: str, root: str) -> list[str]:
    return [match.rstrip(".") for match in re.findall(rf"{re.escape(root.rstrip('/'))}/[\w.\-/]+", text)]
