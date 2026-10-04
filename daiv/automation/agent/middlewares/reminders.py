"""Reminders the harness adds to a model call, saved into the thread just before the reply they produced.

A reminder sent on one call but missing from the next changes history the model already answered: the prompt
cache restarts there, and Claude models that bind thinking blocks to the exact history reject the request.
Saving it through ``ModelResponse.result`` keeps it ahead of the reply; a ``Command`` returned in an
``ExtendedModelResponse`` would be applied after the reply instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import HumanMessage

from automation.agent.synthetic import synthetic_message

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware import ModelRequest


def append_system_reminder(request: ModelRequest, text: str) -> ModelRequest:
    """Return a new request with ``text`` appended as an unsaved reminder message."""
    return request.override(messages=[*request.messages, HumanMessage(content=text)])


def persist_reminder(response: ModelResponse, reminder: HumanMessage) -> ModelResponse:
    """``response`` with ``reminder`` saved ahead of the reply it produced."""
    return ModelResponse(result=[reminder, *response.result], structured_response=response.structured_response)


async def call_with_reminder(
    request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]], text: str, *, kind: str
) -> ModelResponse:
    """Send ``request`` with ``text`` appended as a synthetic reminder and save that reminder ahead of the reply."""
    reminder = synthetic_message(text, kind=kind)
    response = await handler(request.override(messages=[*request.messages, reminder]))
    return persist_reminder(response, reminder)
