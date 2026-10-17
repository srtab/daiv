from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Literal

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage

from automation.agent.middlewares.reminders import call_with_reminder
from automation.agent.synthetic import is_synthetic
from automation.agent.utils import streamed_assistant_message

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

_READ_WINDOW = re.compile(r"@@ lines ([0-9]+)-([0-9]+)(?: of ([0-9]+))? \| next offset ([0-9]+) @@")


def _tool_signature(message: AIMessage) -> tuple[tuple[str, str], ...] | None:
    """Order-independent signature of a message's tool calls, or ``None`` if it has none."""
    if not message.tool_calls:
        return None
    return tuple(sorted((tc["name"], json.dumps(tc["args"], sort_keys=True, default=str)) for tc in message.tool_calls))


def repeated_tool_streak(messages: list[AnyMessage]) -> int:
    """Count consecutive trailing ``AIMessage``s whose tool-call signature is identical.

    Scans from the end: ``ToolMessage``s (the results between calls) and DAIV's synthetic messages (saved
    reminders, the issue context) are skipped; an ``AIMessage`` with no tool calls or a different signature, or
    any other message type (a turn boundary), ends the run. Returns 0 when the most recent ``AIMessage`` has no
    tool calls.
    """
    signature: tuple[tuple[str, str], ...] | None = None
    streak = 0
    for message in reversed(messages):
        if isinstance(message, ToolMessage) or is_synthetic(message):
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


def _read_file_paging_hint(messages: list[AnyMessage]) -> str:
    """Suggest a continuation only from the latest single read's matching status header.

    File contents can contain header-like text, so only the leading metadata line is eligible.
    Results with notices before the header, errors, media, or incompatible windows keep the generic
    reminder. This is advice to the model, never an automatic read or a change to the stop budget.
    """
    results: list[ToolMessage] = []
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            results.append(message)
            continue
        if is_synthetic(message):
            continue
        if not isinstance(message, AIMessage) or len(message.tool_calls) != 1:
            return ""
        call = message.tool_calls[0]
        if call["name"] != "read_file":
            return ""
        result = next((result for result in results if result.tool_call_id == call["id"]), None)
        if (
            result is None
            or result.status != "success"
            or result.name not in (None, "read_file")
            or not isinstance(result.content, str)
        ):
            return ""
        window = _READ_WINDOW.fullmatch(result.content.partition("\n")[0])
        if window is None:
            return ""
        args = call["args"]
        offset = args.get("offset", 0)
        if not isinstance(args.get("file_path"), str) or not args["file_path"] or type(offset) is not int or offset < 0:
            return ""
        limit = args.get("limit")
        if "limit" in args and (type(limit) is not int or limit < 1):
            return ""
        try:
            start, end, next_offset = int(window[1]), int(window[2]), int(window[4])
            total = int(window[3]) if window[3] is not None else None
        except ValueError:
            return ""  # Oversized numeric fields are unusable metadata too.
        if (
            start != offset + 1
            or end < start
            or next_offset != end
            or (total is not None and end >= total)
            or (limit is not None and end - offset > limit)
        ):
            return ""
        next_args = {**args, "offset": next_offset}
        return (
            f"If you need more of this file, read the next page with read_file arguments {json.dumps(next_args)}. "
            "For a specific symbol elsewhere, use a focused lookup instead. "
        )
    return ""


class LoopBreakerMiddleware(AgentMiddleware):
    """Detect verbatim tool-call repetition, nudge the model, then stop the run.

    Implemented entirely in ``awrap_model_call`` so it adds no graph node (a ``before_model`` hook
    would inflate the per-turn superstep cost). When the trailing run of identical tool calls
    reaches ``repeat_threshold`` it sends a ``<system-reminder>`` asking the model to change approach
    or finalize, saved into the thread ahead of the reply (see ``reminders``). A repeated paged
    ``read_file`` also gets concrete next-page arguments when its latest result supplies a usable
    leading window header. After ``max_reminders``
    ignored reminders it takes a terminal action:

    - ``terminal="error"`` (subagents): return a tool-call-free ``AIMessage`` framed as a failure.
      It flows back as the task result text the parent reads. The stuck subagent ends cleanly (its
      ``after_agent`` still runs), the parent run is NOT aborted, and the result reads as an error
      rather than an empty/absent report.
    - ``terminal="finalize"`` (parent): return a tool-call-free ``AIMessage`` so the graph routes to
      END and ``after_agent`` hooks (git publish, patch capture) still run.
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
        if repeat_threshold < 1:
            msg = f"repeat_threshold must be >= 1, got {repeat_threshold!r}"
            raise ValueError(msg)
        if max_reminders < 0:
            msg = f"max_reminders must be >= 0, got {max_reminders!r}"
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
            return await self._terminate(streak, label)

        remaining = self._terminal_streak - streak
        logger.info(
            "LoopBreaker: %d identical consecutive '%s' call(s); injecting redirect reminder (%d left).",
            streak,
            label,
            remaining,
        )
        reminder = self._reminder(streak, label, remaining, paging_hint=_read_file_paging_hint(request.messages))
        return await call_with_reminder(request, handler, reminder, kind="loop_breaker")

    def _reminder(self, streak: int, label: str, remaining: int, *, paging_hint: str = "") -> str:
        return (
            "<system-reminder>"
            f"You have called the '{label}' tool {streak} times in a row with identical arguments. "
            "The result will not change. Do not repeat it. Either take a different action toward your "
            "task (a different tool, different arguments, or a different target) or, if you already have "
            "what you need, produce your final answer now. "
            f"{paging_hint}"
            f"This run will be stopped automatically if you repeat it {remaining} more time(s)."
            "</system-reminder>"
        )

    async def _terminate(self, streak: int, label: str) -> ModelCallResult:
        logger.warning(
            "LoopBreaker: %d identical consecutive '%s' call(s) and %d reminders ignored; terminating via '%s'.",
            streak,
            label,
            self.max_reminders,
            self.terminal,
        )
        if self.terminal == "error":
            # An ERROR: sentinel the code-review orchestrator parses, not prose for a human,
            # so it is deliberately not streamed to the chat.
            return AIMessage(
                content=(
                    f"ERROR: stopped after calling '{label}' {streak} times in a row with identical arguments and "
                    "no progress. The task did NOT complete — this is a failure, not a result. Do not treat this as a "
                    "successful run or as 'no findings'; the work is incomplete."
                )
            )
        return await streamed_assistant_message(
            f"I stopped because I called the '{label}' tool {streak} times in a row without making "
            "progress, and could not complete the task."
        )
