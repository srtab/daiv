from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage

from automation.agent.mcp.deferred.prompt import build_deferred_tools_block
from automation.agent.mcp.deferred.search_tool import make_tool_search
from automation.agent.mcp.deferred.state import DeferredMCPToolsState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ModelRequest, ModelResponse

    from automation.agent.mcp.deferred.index import DeferredMCPToolsIndex

logger = logging.getLogger("daiv.tools")


class DeferredMCPToolsMiddleware(AgentMiddleware):
    state_schema = DeferredMCPToolsState

    def __init__(self, index: DeferredMCPToolsIndex, *, top_k_default: int = 5, top_k_max: int = 10) -> None:
        if not 0 < top_k_default <= top_k_max:
            raise ValueError(f"require 0 < top_k_default <= top_k_max, got {top_k_default=} {top_k_max=}")
        super().__init__()
        self._index = index
        self.tools = [make_tool_search(index, top_k_default=top_k_default, top_k_max=top_k_max)]

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        loaded_names: set[str] = request.state.get("loaded_tool_names") or set()
        loaded_tools: list = []
        stale: list[str] = []
        for name in loaded_names:
            entry = self._index.get(name)
            if entry is None:
                stale.append(name)
            else:
                loaded_tools.append(entry.tool)
        if stale:
            logger.warning("Dropping stale loaded_tool_names not present in deferred index: %s", sorted(stale))

        suffix = build_deferred_tools_block(self._index, loaded_names)
        new_system_prompt = request.system_prompt or ""
        if suffix:
            new_system_prompt = f"{new_system_prompt}\n\n{suffix}" if new_system_prompt else suffix

        new_tools = [*request.tools, *self._index.always_loaded_tools(), *loaded_tools]

        response = await handler(request.override(tools=new_tools, system_prompt=new_system_prompt))
        self._inject_corrective_messages(response, loaded_names)
        return response

    def _inject_corrective_messages(self, response: ModelResponse, loaded_names: set[str]) -> None:
        # Without this, the tool node emits a generic "unknown tool" error and the model often retries blindly.
        messages = getattr(response, "messages", None)
        if not messages:
            return
        last = messages[-1]
        if not isinstance(last, AIMessage) or not getattr(last, "tool_calls", None):
            return

        accessible = loaded_names | self._index.always_loaded_names()

        corrective: list[ToolMessage] = []
        for call in last.tool_calls:
            name = call.get("name", "")
            entry = self._index.get(name)
            if entry is None or name in accessible:
                continue
            corrective.append(
                ToolMessage(
                    content=(
                        f"Tool '{name}' is deferred and not yet loaded. "
                        f"Call tool_search with select=['{name}'] first, then retry."
                    ),
                    tool_call_id=call["id"],
                )
            )

        if corrective:
            response.messages = [*messages, *corrective]
