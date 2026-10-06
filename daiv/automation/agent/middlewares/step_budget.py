from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from langchain.agents.middleware import AgentMiddleware
from langgraph.config import get_config

from automation.agent.middlewares.reminders import call_with_reminder

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

type Band = Literal["warn", "finalize"]


class StepBudgetMiddleware(AgentMiddleware):
    """
    Warn the model when the run approaches the graph ``recursion_limit``.

    Without this, the model has zero visibility into its step budget: runs that hit the
    limit raise ``GraphRecursionError`` mid-flight, skipping every ``after_agent`` hook
    (patch capture) and discarding otherwise-finished work.

    Implemented entirely inside ``wrap_model_call`` so it adds no graph node (a
    ``before_model`` hook — e.g. ``ModelCallLimitMiddleware`` — would itself inflate the
    per-turn superstep cost it is trying to guard). Each band (warning, then finalize) is
    announced once, on the first call inside it, and the reminder is saved into the thread
    ahead of the reply it produced (see ``reminders``), so the model keeps seeing it without
    the request changing behind an answer it already gave.

    Budget is measured *per run*, not against the absolute ``langgraph_step``. LangGraph
    applies ``recursion_limit`` relative to the resume point (it sets
    ``stop = resume_step + recursion_limit + 1`` on every entry), so each invocation gets a
    fresh budget. The raw ``langgraph_step`` instead accumulates across every turn on a
    thread (and survives ``/clear``, which cannot reset LangGraph's internal step counter
    under the same ``thread_id``), so comparing it directly to ``recursion_limit`` would trip
    the reminder on the first model call of any long-lived thread. We therefore capture the
    step this run started at (lazily, on the first model call) and count consumption from
    there. ``create_daiv_agent`` binds per-run state (sandbox, checkpointer, context) into the
    middleware stack, so the agent — and this instance — is necessarily rebuilt per invocation;
    the baseline and the bands already sent thus reset each run without needing a graph node.
    """

    def __init__(
        self, warn_remaining_steps: int = WARN_REMAINING_STEPS, finalize_remaining_steps: int = FINALIZE_REMAINING_STEPS
    ):
        super().__init__()
        self.warn_remaining_steps = warn_remaining_steps
        self.finalize_remaining_steps = finalize_remaining_steps
        # Absolute ``langgraph_step`` this run started at, captured lazily on the first model
        # call; consumption is then measured relative to it (see class docstring).
        self._baseline_step: int | None = None
        self._sent_bands: set[Band] = set()

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelCallResult:
        reminder = self._budget_reminder()
        if reminder is None:
            return await handler(request)
        band, text = reminder
        response = await call_with_reminder(request, handler, text, kind="step_budget")
        self._sent_bands.add(band)
        return response

    def _budget_reminder(self) -> tuple[Band, str] | None:
        """The band this superstep is in and its reminder, or ``None`` when far from the limit or already sent."""
        config = get_config()
        limit = config.get("recursion_limit")
        step = config.get("metadata", {}).get("langgraph_step")
        if not limit or step is None:
            return None

        # Anchor the budget to where THIS run started (see class docstring): the first model
        # call records the baseline, and remaining is measured from supersteps consumed since.
        if self._baseline_step is None:
            self._baseline_step = step
        consumed = step - self._baseline_step
        if consumed < 0:
            # langgraph_step below the captured baseline means the per-run-rebuild invariant this
            # relies on has broken (see class docstring). Clamp so the budget reads as full rather
            # than reporting nonsense, and surface the anomaly instead of failing silently.
            logger.warning(
                "langgraph_step=%d is below the captured baseline=%d; treating run budget as full.",
                step,
                self._baseline_step,
            )
            consumed = 0

        remaining = limit - consumed
        band: Band
        if remaining <= self.finalize_remaining_steps:
            band, template = "finalize", BUDGET_FINALIZE
        elif remaining <= self.warn_remaining_steps:
            band, template = "warn", BUDGET_WARNING
        else:
            return None
        if band in self._sent_bands:
            return None

        turns = max(remaining // STEPS_PER_TURN, 1)
        logger.info(
            "Run has consumed %d of %d supersteps (%d remaining); injecting %s budget reminder.",
            consumed,
            limit,
            remaining,
            band,
        )
        return band, template.format(turns=turns)
