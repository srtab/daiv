from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain.agents.middleware import wrap_model_call
from langchain_core.messages import HumanMessage

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware import ModelRequest, ModelResponse
    from langchain.agents.middleware.types import ModelCallResult

logger = logging.getLogger("daiv.agent")

MAX_EMPTY_RESPONSE_RETRIES = 2

EMPTY_RESPONSE_NUDGE = (
    "Your previous response was empty. "
    "Please continue with the task, ensuring you call at least one tool or provide a text response."
)


def _is_empty(response: ModelResponse) -> bool:
    last_msg = response.result[-1]
    return not last_msg.text() and not getattr(last_msg, "tool_calls", None)


@wrap_model_call(name="EnsureNonEmptyResponseMiddleware")  # ty: ignore[invalid-argument-type]  # async is supported at runtime; the protocol only types the sync form
async def ensure_non_empty_response(
    request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
) -> ModelCallResult:
    """
    Retry empty LLM responses (no content and no tool calls) inside the model node.

    Implemented with ``wrap_model_call`` instead of an ``after_model`` hook on purpose:
    hook middlewares add a graph node to every model/tools cycle, raising the superstep
    cost per turn from 2 to 3 and silently cutting the effective tool-call budget under
    ``recursion_limit`` by a third. Retrying within the node costs zero extra supersteps
    and keeps the synthetic no-op tool round-trip out of the persisted history.

    The retry nudge is appended only to the in-flight request; if the model still returns
    an empty response after ``MAX_EMPTY_RESPONSE_RETRIES``, the empty response is returned
    as-is so the agent loop ends gracefully instead of spinning.
    """
    response = await handler(request)

    for attempt in range(1, MAX_EMPTY_RESPONSE_RETRIES + 1):
        if not _is_empty(response):
            return response
        logger.warning(
            "LLM returned an empty response, retrying within the model node (%d/%d).",
            attempt,
            MAX_EMPTY_RESPONSE_RETRIES,
        )
        response = await handler(
            request.override(messages=[*request.messages, HumanMessage(content=EMPTY_RESPONSE_NUDGE)])
        )

    if _is_empty(response):
        logger.error("LLM returned an empty response after %d retries; giving up.", MAX_EMPTY_RESPONSE_RETRIES)
    return response
