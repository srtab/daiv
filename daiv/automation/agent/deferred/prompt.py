from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from automation.agent.deferred.index import DeferredToolsIndex

_INSTRUCTIONS = """\
You have access to additional tools that are deferred — their names and
summaries are listed below but their full schemas are not loaded by default.
To use one, call `tool_search` first to load it. Once loaded, the tool stays
available for the rest of this session and its full schema appears in your
loaded tools.

Prefer `select=[<name>]` with the exact tool name from the list — that is
faster and more precise than a query. Use `query=` only when you can't
identify the right tool from the list and want to search by capability.

Do not call a deferred tool by name without loading it first; the call will fail.
The list below is exhaustive and stable across the conversation — entries do
not disappear once loaded, so use your loaded-tools view (not this list) to
tell what is currently available.

When the user asks what tools or capabilities you have, include the deferred
tools listed below alongside your loaded tools — note they are deferred and
will be loaded on demand.

Examples:
  - tool_search(select=["gitlab"])              # preferred when the name is known
  - tool_search(query="open pull request")      # only when browsing for capability"""


def build_deferred_tools_block(index: DeferredToolsIndex) -> str:
    """Render the deferred-tools advertisement appended to the system prompt.

    The output is intentionally independent of which tools have already been loaded:
    any per-call variation in the system prompt invalidates Anthropic's prompt cache
    from byte 0 (system message is hashed before tools/messages), which in observed
    traces cost ~22k tokens of fresh cache creation per deferred-tool load.
    """
    lines = [f"{entry.name}: {entry.summary}" for entry in index.deferred_entries()]
    if not lines:
        return ""

    body = "\n".join(lines)
    return f"{_INSTRUCTIONS}\n\n<available-deferred-tools>\n{body}\n</available-deferred-tools>"
