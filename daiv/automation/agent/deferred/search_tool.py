from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from langchain.tools import ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from automation.agent.deferred.state import DeferredToolsState  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import Callable

    from langchain_core.tools import BaseTool

    from automation.agent.deferred.index import DeferredToolsIndex

TOOL_SEARCH_NAME = "tool_search"

TOOL_SEARCH_DESCRIPTION = """\
Load deferred tools by exact name or, as a fallback, by keyword search.

Tool names listed in <available-deferred-tools> are deferred — their schemas
are not loaded by default. Loaded tools remain available for the rest of this
session.

Prefer `select=[<name>, ...]` with exact names from <available-deferred-tools>
— that is faster and more precise than a query. Use `query=` only when you
cannot identify the right tool from the list and want to search by capability.

Examples:
  - tool_search(select=["gitlab"])              # preferred when the name is known
  - tool_search(select=["sentry_find_organizations", "sentry_list_issues"])
  - tool_search(query="open pull request")      # only when browsing for capability"""


def make_tool_search(get_index: Callable[[], DeferredToolsIndex], *, top_k_default: int, top_k_max: int) -> BaseTool:
    async def tool_search(
        runtime: ToolRuntime[object, DeferredToolsState],
        select: Annotated[list[str] | None, "Exact tool names to load. Preferred when the name is known."] = None,
        query: Annotated[str, "Keywords describing the capability. Use only when `select` cannot be used."] = "",
        top_k: Annotated[int | None, "Number of search results to return."] = None,
    ) -> Command:
        index = get_index()

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
