from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from automation.agent.mcp.deferred.state import DeferredMCPToolsState  # noqa: TC001

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

    from automation.agent.mcp.deferred.index import DeferredMCPToolsIndex

TOOL_SEARCH_NAME = "tool_search"

TOOL_SEARCH_DESCRIPTION = """\
Load deferred tools by keyword search or by exact name.

Use this when you need a capability that isn't currently available. Tool names
listed in <available-deferred-tools> are deferred — their schemas are not loaded
by default. Call tool_search with a keyword query, or pass exact names in
`select` if you already know what you need. Loaded tools remain available for
the rest of this session.

Examples:
  - tool_search(query="create github issue")
  - tool_search(select=["sentry_find_organizations"])"""


def make_tool_search(index: DeferredMCPToolsIndex, *, top_k_default: int, top_k_max: int) -> BaseTool:
    """Build the `tool_search` tool bound to a specific deferred-tools index.

    The index is captured by closure so the tool can be added to a middleware's
    `tools` attribute without per-call lookup. State (`loaded_tool_names`) is
    read from `runtime.state` and mutated via the returned `Command`.
    """

    async def tool_search(
        query: Annotated[str, "Keywords describing the capability needed (e.g. 'create github issue')."],
        runtime: ToolRuntime[object, DeferredMCPToolsState],
        select: Annotated[
            list[str] | None, "Optional list of exact tool names to load directly, bypassing search."
        ] = None,
        top_k: Annotated[int | None, "Number of search results to return."] = None,
    ) -> Command:
        if select:
            entries = [e for name in select if (e := index.get(name)) is not None]
        else:
            effective_top_k = min(top_k or top_k_default, top_k_max)
            entries = index.search(query, top_k=effective_top_k)

        if not entries:
            return Command(
                update={
                    "messages": [
                        ToolMessage(content="No matching deferred tools found.", tool_call_id=runtime.tool_call_id)
                    ]
                }
            )

        existing = runtime.state.get("loaded_tool_names") or set()
        new_loaded = existing | {entry.name for entry in entries}

        summary = "\n".join(f"- {entry.name}: {(entry.description.splitlines() or [''])[0][:200]}" for entry in entries)
        return Command(
            update={
                "loaded_tool_names": new_loaded,
                "messages": [
                    ToolMessage(content=f"Loaded {len(entries)} tool(s):\n{summary}", tool_call_id=runtime.tool_call_id)
                ],
            }
        )

    return tool(TOOL_SEARCH_NAME, description=TOOL_SEARCH_DESCRIPTION)(tool_search)
