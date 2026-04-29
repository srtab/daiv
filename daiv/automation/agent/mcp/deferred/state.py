from __future__ import annotations

from typing import Annotated, NotRequired

from langchain.agents import AgentState
from langchain.agents.middleware.types import PrivateStateAttr


def union_loaded_tool_names(left: set[str] | None, right: set[str] | None) -> set[str]:
    return (left or set()) | (right or set())


class DeferredMCPToolsState(AgentState):
    loaded_tool_names: NotRequired[Annotated[set[str], PrivateStateAttr, union_loaded_tool_names]]
