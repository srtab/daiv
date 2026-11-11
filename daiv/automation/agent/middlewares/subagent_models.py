from __future__ import annotations

from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware
from langgraph.errors import GraphBubbleUp

from automation.agent.constants import TASK_TOOL_NAME

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware import ModelRequest, ModelResponse
    from langchain.agents.middleware.types import ModelCallResult, ToolCallRequest
    from langchain.chat_models import BaseChatModel
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


class SubagentModelFailoverMiddleware(AgentMiddleware):
    """Serves ``subagent``'s calls on the model it runs on with no configured entry once its configured model fails one,
    for the rest of the run; ``ConfiguredModels.areport_call_failure`` reports it.

    Registered right inside ``ModelFallbackMiddleware`` (``ConfiguredModels.call_failover``), so the chain is the
    configured model, the model it would use with no entry, then the fallbacks, and the middlewares inside it see the
    model that serves the call.
    """

    def __init__(
        self, configured_models: ConfiguredModels, subagent: str, *, configured_model: BaseChatModel, spec: str
    ) -> None:
        super().__init__()
        self._models = configured_models
        self._subagent = subagent
        self._configured_model = configured_model
        self._spec = spec

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelCallResult:
        if request.model is not self._configured_model:
            return await handler(request)
        error: Exception | None = None
        if not self._models.failed(self._spec):
            try:
                return await handler(request)
            except GraphBubbleUp:
                raise
            except Exception as exc:
                # Safe to continue: the subagent's model without the entry serves, as when the entry can't be built.
                error = exc
        served, served_spec = self._models.subagent_model_without_entry(self._subagent)
        await self._models.areport_call_failure(self._subagent, self._spec, error, served_spec=served_spec)
        return await handler(request.override(model=served))
