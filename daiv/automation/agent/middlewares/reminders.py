from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.messages import HumanMessage

if TYPE_CHECKING:
    from langchain.agents.middleware import ModelRequest


def append_system_reminder(request: ModelRequest, text: str) -> ModelRequest:
    """Return a new request with ``text`` appended as an ephemeral reminder message.

    The reminder rides only on the in-flight request (via ``request.override``); it is never
    persisted to conversation state, so it repeats per call while the triggering condition holds
    and never accumulates in history. Shared by ``StepBudgetMiddleware`` (budget warnings) and
    ``LoopBreakerMiddleware`` (repetition warnings).
    """
    return request.override(messages=[*request.messages, HumanMessage(content=text)])
