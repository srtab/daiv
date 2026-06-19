from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Literal

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage

from automation.agent.middlewares.reminders import append_system_reminder

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware import ModelRequest, ModelResponse
    from langchain.agents.middleware.types import ModelCallResult
    from langchain_core.messages import AnyMessage

logger = logging.getLogger("daiv.agent")

# Streak of byte-identical consecutive tool-call turns at which to start nudging the model.
REPEAT_THRESHOLD = 3
# Escalating reminders before the terminal action. With REPEAT_THRESHOLD=3 the terminal action
# fires at a streak of 6 (reminders injected at streaks 3, 4, 5).
MAX_REMINDERS = 3


def _tool_signature(message: AIMessage) -> tuple[tuple[str, str], ...] | None:
    """Order-independent signature of a message's tool calls, or ``None`` if it has none."""
    if not message.tool_calls:
        return None
    return tuple(sorted((tc["name"], json.dumps(tc["args"], sort_keys=True, default=str)) for tc in message.tool_calls))


def repeated_tool_streak(messages: list[AnyMessage]) -> int:
    """Count consecutive trailing ``AIMessage``s whose tool-call signature is identical.

    Scans from the end: ``ToolMessage``s (the results between calls) are skipped; an ``AIMessage``
    with no tool calls or a different signature, or any other message type (a turn boundary), ends
    the run. Returns 0 when the most recent ``AIMessage`` has no tool calls.
    """
    signature: tuple[tuple[str, str], ...] | None = None
    streak = 0
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            continue
        if isinstance(message, AIMessage):
            current = _tool_signature(message)
            if current is None:
                break
            if signature is None:
                signature = current
                streak = 1
            elif current == signature:
                streak += 1
            else:
                break
            continue
        break  # HumanMessage / SystemMessage — a turn boundary
    return streak


def _repeated_tool_label(messages: list[AnyMessage]) -> str:
    """Comma-joined names of the most recent tool call(s), for log/reminder text."""
    for message in reversed(messages):
        if isinstance(message, AIMessage) and message.tool_calls:
            return ", ".join(sorted({tc["name"] for tc in message.tool_calls}))
    return "the same"


class LoopBreakerMiddleware(AgentMiddleware):
    """Detect verbatim tool-call repetition, nudge the model, then stop the run.

    Implemented entirely in ``awrap_model_call`` so it adds no graph node (a ``before_model`` hook
    would inflate the per-turn superstep cost). When the trailing run of identical tool calls
    reaches ``repeat_threshold`` it appends an ephemeral ``<system-reminder>`` (never persisted)
    asking the model to change approach or finalize; after ``max_reminders`` ignored reminders it
    takes a terminal action:

    - ``terminal="error"`` (subagents): return a tool-call-free ``AIMessage`` framed as a failure.
      It flows back as the ``task`` tool's result for general-purpose/explore subagents, or — for
      ``cr-*`` detectors — as the deferred-output text (``DeferredOutputMiddleware`` writes the last
      message when there is no ``structured_response``). The stuck subagent ends cleanly (its
      ``after_agent`` still runs), the parent run is NOT aborted, and the result reads as an error
      rather than "no findings".
    - ``terminal="finalize"`` (parent): return a tool-call-free ``AIMessage`` so the graph routes to
      END and ``after_agent`` hooks (git publish, patch capture, sandbox teardown) still run.
    """

    def __init__(
        self,
        *,
        terminal: Literal["error", "finalize"] = "error",
        repeat_threshold: int = REPEAT_THRESHOLD,
        max_reminders: int = MAX_REMINDERS,
    ) -> None:
        super().__init__()
        if terminal not in ("error", "finalize"):
            msg = f"terminal must be 'error' or 'finalize', got {terminal!r}"
            raise ValueError(msg)
        self.terminal = terminal
        self.repeat_threshold = repeat_threshold
        self.max_reminders = max_reminders

    @property
    def _terminal_streak(self) -> int:
        return self.repeat_threshold + self.max_reminders

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelCallResult:
        streak = repeated_tool_streak(request.messages)
        if streak < self.repeat_threshold:
            return await handler(request)

        label = _repeated_tool_label(request.messages)
        if streak >= self._terminal_streak:
            return self._terminate(streak, label)

        remaining = self._terminal_streak - streak
        logger.info(
            "LoopBreaker: %d identical consecutive '%s' call(s); injecting redirect reminder (%d left).",
            streak,
            label,
            remaining,
        )
        return await handler(append_system_reminder(request, self._reminder(streak, label, remaining)))

    def _reminder(self, streak: int, label: str, remaining: int) -> str:
        return (
            "<system-reminder>"
            f"You have called the '{label}' tool {streak} times in a row with identical arguments. "
            "The result will not change. Do not repeat it. Either take a different action toward your "
            "task (a different tool, different arguments, or a different target) or, if you already have "
            "what you need, produce your final answer now. "
            f"This run will be stopped automatically if you repeat it {remaining} more time(s)."
            "</system-reminder>"
        )

    def _terminate(self, streak: int, label: str) -> ModelCallResult:
        logger.warning(
            "LoopBreaker: %d identical consecutive '%s' call(s) and %d reminders ignored; terminating via '%s'.",
            streak,
            label,
            self.max_reminders,
            self.terminal,
        )
        if self.terminal == "error":
            return AIMessage(
                content=(
                    f"ERROR: stopped after calling '{label}' {streak} times in a row with identical arguments and "
                    "no progress. The task did NOT complete — this is a failure, not a result. Do not treat this as a "
                    "successful run or as 'no findings'; the work is incomplete."
                )
            )
        return AIMessage(
            content=(
                f"I stopped because I called the '{label}' tool {streak} times in a row without making "
                "progress, and could not complete the task."
            )
        )
