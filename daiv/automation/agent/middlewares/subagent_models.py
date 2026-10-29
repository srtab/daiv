from __future__ import annotations

from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware

from automation.agent.constants import TASK_TOOL_NAME

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware.types import ToolCallRequest
    from langchain_core.messages import ToolMessage
    from langgraph.types import Command

    from automation.agent.configured_models import ConfiguredModels


class SubagentModelNoticesMiddleware(AgentMiddleware):
    """Reports a subagent's unusable configured model when the ``task`` tool dispatches it, not on every run that
    builds it (``ConfiguredModels.areport_dispatch``)."""

    def __init__(self, configured_models: ConfiguredModels) -> None:
        super().__init__()
        self._models = configured_models

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        if request.tool_call["name"] == TASK_TOOL_NAME:
            await self._models.areport_dispatch(request.tool_call["args"].get("subagent_type", ""))
        return await handler(request)
