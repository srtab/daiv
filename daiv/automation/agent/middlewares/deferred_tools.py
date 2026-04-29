from __future__ import annotations

from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware

from automation.agent.mcp.deferred.prompt import build_deferred_tools_block
from automation.agent.mcp.deferred.search_tool import make_tool_search
from automation.agent.mcp.deferred.state import DeferredMCPToolsState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ModelRequest, ModelResponse

    from automation.agent.mcp.deferred.index import DeferredMCPToolsIndex


class DeferredMCPToolsMiddleware(AgentMiddleware):
    """Lazily expose MCP tools to the agent via `tool_search`.

    Each model call extends request.tools with:
      1. Always-loaded MCP tools from the index.
      2. Tools the agent has previously discovered (`loaded_tool_names` in state).
    And appends to the system prompt:
      3. A `<available-deferred-tools>` block listing unloaded tool names with
         usage instructions for `tool_search`.
    """

    state_schema = DeferredMCPToolsState

    def __init__(self, index: DeferredMCPToolsIndex, *, top_k_default: int = 5, top_k_max: int = 10) -> None:
        super().__init__()
        self._index = index
        self.tools = [make_tool_search(index, top_k_default=top_k_default, top_k_max=top_k_max)]

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        loaded_names: set[str] = request.state.get("loaded_tool_names") or set()
        loaded_tools = [entry.tool for name in loaded_names if (entry := self._index.get(name)) is not None]

        suffix = build_deferred_tools_block(self._index, loaded_names)
        new_system_prompt = request.system_prompt or ""
        if suffix:
            new_system_prompt = f"{new_system_prompt}\n\n{suffix}" if new_system_prompt else suffix

        # request.tools contains no MCP tools when the deferred flag is on — see graph.py wiring.
        new_tools = [*request.tools, *self._index.always_loaded_tools(), *loaded_tools]

        return await handler(request.override(tools=new_tools, system_prompt=new_system_prompt))
