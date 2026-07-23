from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage
from langgraph.config import get_config

from automation.agent.middlewares.reminders import append_system_reminder

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

HEARTBEAT_TOP_REREADS = 3
HEARTBEAT_REREAD_FLOOR = 3

HEARTBEAT_REMINDER = (
    "<system-reminder>"
    "Progress check: you have made {calls} model calls in this run.{files_part} "
    "Re-reading a file you have already read rarely yields new information. "
    "Briefly state in text what you have concluded so far and what remains, then take the "
    "smallest next step. If you already have what you need, produce your final output now."
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

    Optionally also injects a periodic state-aware heartbeat every ``heartbeat_every_calls``
    model calls (used by subagent stacks) so a long-running or pattern-locked subagent is
    periodically re-grounded; budget reminders take precedence when both would fire.

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
    the baseline thus resets each run without needing a graph node.
    """

    def __init__(
        self,
        warn_remaining_steps: int = WARN_REMAINING_STEPS,
        finalize_remaining_steps: int = FINALIZE_REMAINING_STEPS,
        *,
        heartbeat_every_calls: int | None = None,
    ):
        super().__init__()
        if heartbeat_every_calls is not None and heartbeat_every_calls < 1:
            msg = f"heartbeat_every_calls must be >= 1 when set, got {heartbeat_every_calls!r}"
            raise ValueError(msg)
        self.warn_remaining_steps = warn_remaining_steps
        self.finalize_remaining_steps = finalize_remaining_steps
        self.heartbeat_every_calls = heartbeat_every_calls
        # Absolute ``langgraph_step`` this run started at, captured lazily on the first model
        # call; consumption is then measured relative to it (see class docstring).
        self._baseline_step: int | None = None
        # Model calls seen by this instance; drives the periodic heartbeat cadence.
        self._model_calls = 0

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelCallResult:
        self._model_calls += 1
        reminder = self._budget_reminder()
        if reminder is None and self.heartbeat_every_calls and self._model_calls % self.heartbeat_every_calls == 0:
            reminder = self._heartbeat_reminder(request)
        if reminder is None:
            return await handler(request)
        return await handler(append_system_reminder(request, reminder))

    def _budget_reminder(self) -> str | None:
        """Build the budget reminder for the current superstep, or ``None`` when far from the limit."""
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
        if remaining <= self.finalize_remaining_steps:
            template = BUDGET_FINALIZE
        elif remaining <= self.warn_remaining_steps:
            template = BUDGET_WARNING
        else:
            return None

        turns = max(remaining // STEPS_PER_TURN, 1)
        logger.info(
            "Run has consumed %d of %d supersteps (%d remaining); injecting budget reminder.",
            consumed,
            limit,
            remaining,
        )
        return template.format(turns=turns)

    def _heartbeat_reminder(self, request: ModelRequest) -> str:
        """State-aware periodic reminder: model-call count plus this run's file-read stats.

        The stats make the reminder out-of-distribution for a pattern-locked model — naming the
        file it keeps re-reading is what breaks the attractor; a generic "stay on track" would
        just be absorbed into the loop. Budget reminders take precedence (see the caller):
        near the limit, "finalize now" is the more urgent signal.
        """
        reads: Counter[str] = Counter()
        for message in request.messages:
            if isinstance(message, AIMessage):
                for tool_call in message.tool_calls or []:
                    if tool_call["name"] == "read_file" and (path := tool_call["args"].get("file_path")):
                        reads[path] += 1
        files_part = ""
        if reads:
            top = ", ".join(
                f"{path.rsplit('/', 1)[-1]} ({count}x)"
                for path, count in reads.most_common(HEARTBEAT_TOP_REREADS)
                if count >= HEARTBEAT_REREAD_FLOOR
            )
            files_part = f" You have read {len(reads)} distinct file(s)" + (f"; most re-read: {top}." if top else ".")
        logger.info("Injecting heartbeat reminder at model call %d.", self._model_calls)
        return HEARTBEAT_REMINDER.format(calls=self._model_calls, files_part=files_part)
