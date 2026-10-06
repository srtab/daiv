from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain.agents.middleware import wrap_model_call

from automation.agent.middlewares.reminders import persist_reminder
from automation.agent.synthetic import synthetic_message

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware import ModelRequest, ModelResponse
    from langchain.agents.middleware.types import ModelCallResult
    from langchain_core.messages import HumanMessage

logger = logging.getLogger("daiv.agent")

MAX_EMPTY_RESPONSE_RETRIES = 2

EMPTY_RESPONSE_NUDGE = (
    "Your previous response was empty. "
    "Please continue with the task, ensuring you call at least one tool or provide a text response."
)


def _is_empty(response: ModelResponse) -> bool:
    last_msg = response.result[-1]
    return not last_msg.text and not getattr(last_msg, "tool_calls", None)


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

    Each retry sends the original request plus one nudge, the same message on every retry.
    When a retry was sent, the nudge is saved into the thread ahead of the reply it produced
    (see ``reminders``); the discarded empty replies are never saved. If the model still
    returns an empty response after ``MAX_EMPTY_RESPONSE_RETRIES``, that empty response is
    returned, behind the saved nudge, so the agent loop ends gracefully instead of spinning.
    """
    response = await handler(request)
    nudge: HumanMessage | None = None

    for attempt in range(1, MAX_EMPTY_RESPONSE_RETRIES + 1):
        if not _is_empty(response):
            break
        logger.warning(
            "LLM returned an empty response, retrying within the model node (%d/%d).",
            attempt,
            MAX_EMPTY_RESPONSE_RETRIES,
        )
        nudge = nudge or synthetic_message(EMPTY_RESPONSE_NUDGE, kind="empty_response")
        response = await handler(request.override(messages=[*request.messages, nudge]))

    if _is_empty(response):
        logger.error("LLM returned an empty response after %d retries; giving up.", MAX_EMPTY_RESPONSE_RETRIES)
    return response if nudge is None else persist_reminder(response, nudge)
