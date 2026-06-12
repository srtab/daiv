from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage
from langgraph.config import get_config

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from langchain.agents.middleware import ModelRequest, ModelResponse
    from langchain.agents.middleware.types import ModelCallResult

logger = logging.getLogger("daiv.agent")

# One model/tools cycle costs 2 supersteps as long as no per-turn hook middleware
# (`before_model`/`after_model`) is registered — keep them out of the per-turn path.
STEPS_PER_TURN = 2

WARN_REMAINING_STEPS = 40
FINALIZE_REMAINING_STEPS = 16

BUDGET_WARNING = (
    "<system-reminder>"
    "Step budget: roughly {turns} tool-call turns remain before this run is hard-stopped. "
    "Prioritize completing the core task. If your change is already implemented and the directly "
    "relevant verification has passed, finalize your answer now — skip optional polish, repeated "
    "test-suite runs, and investigations of pre-existing issues."
    "</system-reminder>"
)

BUDGET_FINALIZE = (
    "<system-reminder>"
    "Step budget exhausted: at most {turns} tool-call turns remain before this run is hard-stopped "
    "and all unsaved work is lost. Stop calling tools unless strictly necessary to persist your work, "
    "and produce your final answer NOW."
    "</system-reminder>"
)


class StepBudgetMiddleware(AgentMiddleware):
    """
    Warn the model when the run approaches the graph ``recursion_limit``.

    Without this, the model has zero visibility into its step budget: runs that hit the
    limit raise ``GraphRecursionError`` mid-flight, skipping every ``after_agent`` hook
    (patch capture, sandbox teardown) and discarding otherwise-finished work.

    Implemented entirely inside ``wrap_model_call`` so it adds no graph node (a
    ``before_model`` hook — e.g. ``ModelCallLimitMiddleware`` — would itself inflate the
    per-turn superstep cost it is trying to guard). The reminder is appended only to the
    in-flight request and never persisted to the conversation state, so it repeats on
    every call while the budget stays low.
    """

    def __init__(
        self, warn_remaining_steps: int = WARN_REMAINING_STEPS, finalize_remaining_steps: int = FINALIZE_REMAINING_STEPS
    ):
        super().__init__()
        self.warn_remaining_steps = warn_remaining_steps
        self.finalize_remaining_steps = finalize_remaining_steps

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelCallResult:
        reminder = self._budget_reminder()
        if reminder is None:
            return await handler(request)
        return await handler(request.override(messages=[*request.messages, HumanMessage(content=reminder)]))

    def _budget_reminder(self) -> str | None:
        """Build the budget reminder for the current superstep, or ``None`` when far from the limit."""
        config = get_config()
        limit = config.get("recursion_limit")
        step = config.get("metadata", {}).get("langgraph_step")
        if not limit or step is None:
            return None

        remaining = limit - step
        if remaining <= self.finalize_remaining_steps:
            template = BUDGET_FINALIZE
        elif remaining <= self.warn_remaining_steps:
            template = BUDGET_WARNING
        else:
            return None

        turns = max(remaining // STEPS_PER_TURN, 1)
        logger.info("Run is %d supersteps away from recursion_limit=%d; injecting budget reminder.", remaining, limit)
        return template.format(turns=turns)
