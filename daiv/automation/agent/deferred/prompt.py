from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from automation.agent.deferred.index import DeferredToolsIndex

_INSTRUCTIONS = """\
You have access to additional tools that are deferred — their names and
summaries are listed below but their full schemas are not loaded by default.
To use one, call `tool_search` first to load it. Once loaded, the tool stays
available for the rest of this session.

Prefer `select=[<name>]` with the exact tool name from the list — that is
faster and more precise than a query. Use `query=` only when you can't
identify the right tool from the list and want to search by capability.

Do not call a deferred tool by name without loading it first; the call will fail.

When the user asks what tools or capabilities you have, include the deferred
tools listed below alongside your loaded tools — note they are deferred and
will be loaded on demand.

Examples:
  - tool_search(select=["gitlab"])              # preferred when the name is known
  - tool_search(query="open pull request")      # only when browsing for capability"""


def build_deferred_tools_block(index: DeferredToolsIndex, loaded: set[str]) -> str:
    lines = [f"{entry.name}: {entry.summary}" for entry in index.deferred_entries() if entry.name not in loaded]
    if not lines:
        return ""

    body = "\n".join(lines)
    return f"{_INSTRUCTIONS}\n\n<available-deferred-tools>\n{body}\n</available-deferred-tools>"
