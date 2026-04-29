from __future__ import annotations

from typing import Annotated, NotRequired

from langchain.agents import AgentState
from langchain.agents.middleware.types import PrivateStateAttr


def union_loaded_tool_names(left: set[str] | None, right: set[str] | None) -> set[str]:
    """Reducer that merges two `loaded_tool_names` sets via union."""
    return (left or set()) | (right or set())


class DeferredMCPToolsState(AgentState):
    """Agent state extension tracking MCP tools that have been lazily loaded.

    `loaded_tool_names` persists across turns (via the checkpointer) so once a
    tool is discovered via `tool_search`, it stays in `request.tools` for the
    remainder of the thread without re-discovery.
    """

    loaded_tool_names: NotRequired[Annotated[set[str], PrivateStateAttr, union_loaded_tool_names]]
