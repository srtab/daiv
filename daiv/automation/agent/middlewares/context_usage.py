from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware
from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.messages import AIMessage

from automation.agent.events import CONTEXT_USAGE_EVENT, context_usage_payload
from automation.agent.usage_tracking import resolve_context_window

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware import ModelRequest, ModelResponse
    from langchain.agents.middleware.types import ModelCallResult

logger = logging.getLogger("daiv.agent")


class ContextUsageMiddleware(AgentMiddleware):
    """Emit :data:`CONTEXT_USAGE_EVENT` after each main-model call, for the chat's context meter.

    ``awrap_model_call`` rather than a hook: a ``before_model``/``after_model`` middleware adds a
    graph node and raises the per-turn superstep cost from 2 to 3 (see ``StepBudgetMiddleware``).
    Must be registered after ``ModelFallbackMiddleware``: the fallback retries the inner chain
    with ``request.override(model=...)``, so only a middleware inside it sees the model that
    actually served the call — listed before it, a fallback-served call would divide one model's
    usage by another model's window.

    A middleware and not a usage callback on purpose: the callback is ContextVar-inheritable
    (that is how cost aggregates across subagents), so its last-call state can report a
    subagent's context as the thread's. This hook only ever sees the host agent's model node;
    side-runs never pass through it, and a subagent's own stack does not include it.
    """

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelCallResult:
        response = await handler(request)
        try:
            await self._dispatch_usage(request, response)
        except Exception:
            # Best-effort, like streamed_assistant_message: a cosmetic frame must never
            # fail the model call that produced it.
            logger.warning("Could not stream context usage; the meter catches up next call", exc_info=True)
        return response

    async def _dispatch_usage(self, request: ModelRequest, response: ModelResponse) -> None:
        message = response.result[-1] if response.result else None
        if not isinstance(message, AIMessage):
            return
        usage = message.usage_metadata
        model_name = (message.response_metadata or {}).get("model_name")
        if not usage or not model_name:
            return
        window = resolve_context_window(request.model, model_name)
        await adispatch_custom_event(
            CONTEXT_USAGE_EVENT,
            context_usage_payload(
                model=model_name,
                input_tokens=usage.get("input_tokens", 0),
                output_tokens=usage.get("output_tokens", 0),
                cached_tokens=(usage.get("input_token_details") or {}).get("cache_read", 0),
                window_tokens=window.tokens if window else None,
                window_source=window.source if window else None,
            ),
        )
