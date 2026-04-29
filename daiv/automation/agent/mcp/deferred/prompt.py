from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from automation.agent.mcp.deferred.index import DeferredMCPToolsIndex

_FIRST_LINE_CAP = 200

_INSTRUCTIONS = """\
You have access to additional tools that are deferred — their names are listed
below but their full schemas are not loaded by default. To use any of them, call
`tool_search` first with a keyword query, or with the exact name in `select`.
Once loaded, the tool stays available for the rest of this session. Do not
attempt to call a deferred tool by name without loading it first; the call will
fail.

Example: if asked to "open a PR for me", call
tool_search(query="create pull request github") before attempting the action."""


def build_deferred_tools_block(index: DeferredMCPToolsIndex, loaded: set[str]) -> str:
    """Render the `<available-deferred-tools>` system-prompt suffix.

    Skips tools that are already loaded (they appear in `request.tools` with
    full schemas; listing them here is redundant noise).
    """
    lines: list[str] = []
    for entry in index.deferred_entries():
        if entry.name in loaded:
            continue
        first_line = (entry.description.splitlines() or [""])[0][:_FIRST_LINE_CAP]
        lines.append(f"{entry.name}: {first_line}")

    if not lines:
        return ""

    body = "\n".join(lines)
    return f"{_INSTRUCTIONS}\n\n<available-deferred-tools>\n{body}\n</available-deferred-tools>"
