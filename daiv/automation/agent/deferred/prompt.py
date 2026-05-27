from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from automation.agent.deferred.index import DeferredToolsIndex

_INSTRUCTIONS = """\
## `tool_search` (deferred tools)

Some tools are deferred — only their names and summaries are loaded into your context. Their full schemas are not loaded until you call `tool_search` to load them. Once loaded, the tool stays available for the rest of the session and appears in your loaded tools.

How to use:
- Prefer `select` (a JSON array of exact tool names from `<available-deferred-tools>` below) — fastest and most precise.
- Use `query` (a keyword string) only when you can't identify the right tool from its name.
- Do NOT call a deferred tool by name without loading it first — the call will fail.
- `select` must be an array, not a string. `["gitlab"]` is correct; `"[\"gitlab\"]"` or `"gitlab"` is not.

Notes:
- The list below is exhaustive and stable across the conversation. Use your loaded-tools view (not this list) to tell what is currently available.
- When the user asks what tools or capabilities you have, include the deferred tools alongside your loaded tools, marked as deferred.

<example>
{"name": "tool_search", "arguments": {"select": ["gitlab"]}}            # preferred when the name is known
{"name": "tool_search", "arguments": {"query": "open pull request"}}    # only when browsing by capability
</example>"""  # noqa: E501


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
