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
  - tool_search(query="", select=["sentry_find_organizations"])"""


def make_tool_search(index: DeferredMCPToolsIndex, *, top_k_default: int, top_k_max: int) -> BaseTool:
    async def tool_search(
        query: Annotated[str, "Keywords describing the capability needed (e.g. 'create github issue')."],
        runtime: ToolRuntime[object, DeferredMCPToolsState],
        select: Annotated[
            list[str] | None, "Optional list of exact tool names to load directly, bypassing search."
        ] = None,
        top_k: Annotated[int | None, "Number of search results to return."] = None,
    ) -> Command:
        missing: list[str] = []
        if select:
            entries = []
            for name in select:
                entry = index.get(name)
                if entry is None:
                    missing.append(name)
                else:
                    entries.append(entry)
        else:
            effective_top_k = min(top_k or top_k_default, top_k_max)
            entries = index.search(query, top_k=effective_top_k)

        if not entries:
            if select and missing:
                content = f"None of the requested names are deferred tools: {', '.join(missing)}."
            elif select:
                content = "Empty `select` list — pass at least one tool name."
            else:
                content = f"No deferred tools matched query {query!r}."
            return Command(update={"messages": [ToolMessage(content=content, tool_call_id=runtime.tool_call_id)]})

        existing = runtime.state.get("loaded_tool_names") or set()
        new_loaded = existing | {entry.name for entry in entries}

        summary = "\n".join(f"- {entry.name}: {entry.summary}" for entry in entries)
        content = f"Loaded {len(entries)} tool(s):\n{summary}"
        if missing:
            content += f"\n\nIgnored unknown names: {', '.join(missing)}"
        return Command(
            update={
                "loaded_tool_names": new_loaded,
                "messages": [ToolMessage(content=content, tool_call_id=runtime.tool_call_id)],
            }
        )

    return tool(TOOL_SEARCH_NAME, description=TOOL_SEARCH_DESCRIPTION)(tool_search)
