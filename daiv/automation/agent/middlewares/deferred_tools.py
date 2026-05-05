from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage

from automation.agent.deferred.index import DeferredToolsIndex
from automation.agent.deferred.prompt import build_deferred_tools_block
from automation.agent.deferred.search_tool import TOOL_SEARCH_NAME, make_tool_search
from automation.agent.deferred.state import DeferredToolsState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from langchain.agents.middleware.types import ModelRequest, ModelResponse
    from langchain_core.tools import BaseTool

logger = logging.getLogger("daiv.tools")


class DeferredToolsMiddleware(AgentMiddleware):
    """Defers every tool not in ``always_loaded`` behind a ``tool_search`` capability.

    On the first model call, the middleware snapshots the union of ``request.tools`` and
    ``extra_tools``, sends only the always-loaded tools (plus already-loaded deferred ones)
    to the model, and indexes the rest for ``tool_search``. Subsequent calls reuse the index.
    """

    state_schema = DeferredToolsState

    def __init__(
        self,
        *,
        always_loaded: Iterable[str],
        extra_tools: Iterable[BaseTool] = (),
        top_k_default: int = 5,
        top_k_max: int = 10,
    ) -> None:
        if not 0 < top_k_default <= top_k_max:
            raise ValueError(f"require 0 < top_k_default <= top_k_max, got {top_k_default=} {top_k_max=}")
        super().__init__()
        # tool_search itself is always loaded — without it the agent can't load deferred tools.
        self._always_loaded: set[str] = {*always_loaded, TOOL_SEARCH_NAME}
        self._extra_tools: list[BaseTool] = list(extra_tools)
        self._index: DeferredToolsIndex | None = None
        # Expose extra_tools to the agent factory so they are registered with the runtime ToolNode
        # at build time (langchain/agents/factory.py collects middleware.tools into available_tools).
        # awrap_model_call still filters them out of the model's view until they're loaded via
        # tool_search — registration here only makes them executable when the model calls them.
        self.tools = [
            make_tool_search(self._get_index, top_k_default=top_k_default, top_k_max=top_k_max),
            *self._extra_tools,
        ]

    def _get_index(self) -> DeferredToolsIndex:
        if self._index is None:
            # Defensive: tool_search shouldn't be reachable before the first awrap_model_call,
            # since the model can't call it until the wrapped handler runs at least once.
            return DeferredToolsIndex([])
        return self._index

    def _build_index(self, request_tools: Iterable[BaseTool]) -> DeferredToolsIndex:
        seen: set[str] = set()
        deferred: list[BaseTool] = []
        for tool in (*request_tools, *self._extra_tools):
            if tool.name in seen or tool.name in self._always_loaded:
                seen.add(tool.name)
                continue
            seen.add(tool.name)
            deferred.append(tool)
        return DeferredToolsIndex(deferred)

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        if self._index is None:
            self._index = self._build_index(request.tools)

        loaded_names: set[str] = request.state.get("loaded_tool_names") or set()
        loaded_tools: list[BaseTool] = []
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

        # Filter request.tools to always-loaded ones; the rest are deferred.
        allowed: list[BaseTool] = []
        seen_names: set[str] = set()
        for tool in request.tools:
            if tool.name in self._always_loaded and tool.name not in seen_names:
                allowed.append(tool)
                seen_names.add(tool.name)
        # Surface any always-loaded extra_tools the request doesn't already carry.
        for tool in self._extra_tools:
            if tool.name in self._always_loaded and tool.name not in seen_names:
                allowed.append(tool)
                seen_names.add(tool.name)

        new_tools = [*allowed, *loaded_tools]

        response = await handler(request.override(tools=new_tools, system_prompt=new_system_prompt))
        self._inject_corrective_messages(response, loaded_names)
        return response

    def _inject_corrective_messages(self, response: ModelResponse, loaded_names: set[str]) -> None:
        # Without this, the tool node emits a generic "unknown tool" error and the model often retries blindly.
        messages = getattr(response, "messages", None)
        if not messages or self._index is None:
            return
        last = messages[-1]
        if not isinstance(last, AIMessage) or not getattr(last, "tool_calls", None):
            return

        accessible = loaded_names | self._always_loaded

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
