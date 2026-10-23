import re
from unittest.mock import patch

import pytest
from deepagents import create_deep_agent
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, ModelRequest, TodoListMiddleware
from langchain.agents.middleware.types import ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError

from automation.agent.middlewares.step_budget import StepBudgetMiddleware
from automation.agent.synthetic import is_synthetic


def _request() -> ModelRequest:
    return ModelRequest(model=GenericFakeChatModel(messages=iter([])), messages=[HumanMessage(content="hi")])


def _config(recursion_limit: int | None, step: int | None) -> dict:
    config: dict = {"metadata": {}}
    if recursion_limit is not None:
        config["recursion_limit"] = recursion_limit
    if step is not None:
        config["metadata"]["langgraph_step"] = step
    return config


def _patched_config(recursion_limit: int | None, step: int | None):
    return patch("automation.agent.middlewares.step_budget.get_config", return_value=_config(recursion_limit, step))


def _middleware(baseline: int | None = 0, steps_per_turn: int = 2, setup_steps: int = 0) -> StepBudgetMiddleware:
    """A fresh middleware whose run started at ``baseline`` (default 0, i.e. a fresh thread).

    ``None`` leaves the baseline uncaptured so the next ``_budget_reminder`` call records it,
    mirroring the lazy capture on the first model call of a run.
    """
    middleware = StepBudgetMiddleware(warn_remaining_turns=20, finalize_remaining_turns=8)
    middleware.steps_per_turn = steps_per_turn
    middleware.setup_steps = setup_steps
    middleware._baseline_step = baseline
    return middleware


class _BeforeModelHook(AgentMiddleware):
    async def abefore_model(self, state, runtime):
        return None


class _BeforeAgentHook(AgentMiddleware):
    async def abefore_agent(self, state, runtime):
        return None


class _LoopingModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _looping_model() -> _LoopingModel:
    calls = (AIMessage(content="", tool_calls=[{"name": "ping", "args": {}, "id": f"call_{i}"}]) for i in range(500))
    return _LoopingModel(messages=calls)


class TestStepBudgetMiddleware:
    def test_no_reminder_far_from_limit(self):
        with _patched_config(recursion_limit=500, step=100):
            assert _middleware()._budget_reminder() is None

    def test_warning_reminder_in_warn_zone(self):
        with _patched_config(recursion_limit=500, step=470):
            band, text = _middleware()._budget_reminder()
        assert band == "warn"
        # 30 supersteps left ≈ 15 turns
        assert "15 tool-call turns" in text
        assert "hard-stopped" in text

    def test_finalize_reminder_near_limit(self):
        with _patched_config(recursion_limit=500, step=490):
            band, text = _middleware()._budget_reminder()
        assert band == "finalize"
        assert "NOW" in text

    def test_counts_turns_at_the_graph_superstep_cost(self):
        # 60 supersteps left at 4 per turn is 15 turns: inside the warning band, which counting
        # 2 per turn (30 turns) would miss.
        with _patched_config(recursion_limit=500, step=440):
            band, text = _middleware(steps_per_turn=4)._budget_reminder()
        assert band == "warn"
        assert "15 tool-call turns" in text

    def test_finalize_band_is_measured_in_turns(self):
        with _patched_config(recursion_limit=500, step=468):
            band, text = _middleware(steps_per_turn=4)._budget_reminder()
        assert band == "finalize"
        assert "at most 8 tool-call turns" in text

    def test_setup_steps_count_against_the_turns_left(self):
        # The PR #1616 run: 6 before_agent hooks, 4 supersteps per turn, 16 left since the baseline.
        with _patched_config(recursion_limit=500, step=484):
            band, text = _middleware(steps_per_turn=4, setup_steps=6)._budget_reminder()
        assert band == "finalize"
        assert "at most 2 tool-call turns" in text

    @pytest.mark.parametrize("recursion_limit", [32, 80])
    def test_a_low_limit_gets_no_reminder_on_its_first_call(self, recursion_limit):
        with _patched_config(recursion_limit=recursion_limit, step=0):
            assert _middleware(steps_per_turn=4)._budget_reminder() is None

    def test_unbound_middleware_logs_once_and_sends_nothing(self, caplog):
        middleware = StepBudgetMiddleware()
        with _patched_config(recursion_limit=500, step=0):
            assert middleware._budget_reminder() is None
        with _patched_config(recursion_limit=500, step=495):
            assert middleware._budget_reminder() is None

        assert [r.levelname for r in caplog.records] == ["ERROR"]

    def test_no_reminder_without_recursion_limit_in_config(self):
        with _patched_config(recursion_limit=None, step=470):
            assert _middleware()._budget_reminder() is None

    def test_no_reminder_without_step_in_config(self):
        with _patched_config(recursion_limit=500, step=None):
            assert _middleware()._budget_reminder() is None

    def test_resumed_run_starts_with_full_budget(self):
        # A thread that has already accumulated supersteps across prior turns resumes at a high
        # absolute langgraph_step, but recursion_limit is applied relative to the resume point, so
        # the run still has a full budget. The first model call must not trip the reminder.
        middleware = _middleware(baseline=None)
        with _patched_config(recursion_limit=500, step=467):
            assert middleware._budget_reminder() is None

    def test_resumed_run_warns_after_consuming_budget(self):
        # Same resumed thread (baseline 467): once THIS run has consumed exactly 470 supersteps
        # the warning fires, proving consumption is measured per-run, not against absolute step.
        middleware = _middleware(baseline=None)
        with _patched_config(recursion_limit=500, step=467):
            assert middleware._budget_reminder() is None  # captures baseline = 467
        with _patched_config(recursion_limit=500, step=937):  # consumed 470 → 30 remaining
            reminder = middleware._budget_reminder()
        assert reminder is not None
        assert "15 tool-call turns" in reminder[1]

    def test_baseline_captured_once_and_not_re_anchored(self):
        # The baseline is anchored on the first call and never re-captured, so consumption keeps
        # growing (remaining shrinking) as langgraph_step advances. Pins the "capture once"
        # contract: an unconditional re-assignment would reset consumed to 0 every call and
        # silently disable the warning for the whole run.
        middleware = _middleware(baseline=None)
        with _patched_config(recursion_limit=500, step=467):
            assert middleware._budget_reminder() is None  # baseline = 467, consumed 0
        with _patched_config(recursion_limit=500, step=917):
            assert middleware._budget_reminder() is None  # consumed 450 → 50 remaining, still quiet
        with _patched_config(recursion_limit=500, step=937):
            assert middleware._budget_reminder() is not None  # consumed 470 → 30 remaining → warn
        assert middleware._baseline_step == 467  # never re-anchored

    def test_step_below_baseline_fails_safe_to_full_budget(self):
        # Defensive: if langgraph_step is ever observed below the captured baseline (the per-run
        # rebuild invariant broke), consumed is clamped to 0 so we never spuriously tell the model
        # to finalize. The original baseline is left untouched.
        middleware = _middleware(baseline=500)
        with _patched_config(recursion_limit=500, step=470):  # raw consumed would be -30
            assert middleware._budget_reminder() is None
        assert middleware._baseline_step == 500

    async def test_wrap_model_call_sends_and_saves_the_reminder(self):
        seen_requests = []

        async def handler(request: ModelRequest) -> ModelResponse:
            seen_requests.append(request)
            return ModelResponse(result=[AIMessage(content="ok")])

        request = _request()
        with _patched_config(recursion_limit=500, step=470):
            response = await _middleware().awrap_model_call(request, handler)

        sent = seen_requests[0].messages[-1]
        assert "tool-call turns" in sent.content
        assert response.result[0] is sent
        assert is_synthetic(sent)
        assert response.result[1].content == "ok"
        assert len(request.messages) == 1

    async def test_wrap_model_call_passes_request_through_far_from_limit(self):
        seen_requests = []

        async def handler(request: ModelRequest) -> ModelResponse:
            seen_requests.append(request)
            return ModelResponse(result=[AIMessage(content="ok")])

        request = _request()
        with _patched_config(recursion_limit=500, step=100):
            await _middleware().awrap_model_call(request, handler)

        assert seen_requests[0] is request

    async def test_each_band_is_sent_once(self):
        seen_requests = []

        async def handler(request: ModelRequest) -> ModelResponse:
            seen_requests.append(request)
            return ModelResponse(result=[AIMessage(content="ok")])

        middleware = _middleware()
        with _patched_config(recursion_limit=500, step=470):
            await middleware.awrap_model_call(_request(), handler)
        second_request = _request()
        with _patched_config(recursion_limit=500, step=472):
            second_response = await middleware.awrap_model_call(second_request, handler)

        assert seen_requests[1] is second_request
        assert len(second_response.result) == 1

    async def test_finalize_band_still_fires_after_the_warning(self):
        async def handler(request: ModelRequest) -> ModelResponse:
            return ModelResponse(result=[AIMessage(content="ok")])

        middleware = _middleware()
        with _patched_config(recursion_limit=500, step=470):
            await middleware.awrap_model_call(_request(), handler)
        with _patched_config(recursion_limit=500, step=490):
            response = await middleware.awrap_model_call(_request(), handler)

        assert "NOW" in response.result[0].content

    async def test_band_is_resent_when_the_model_call_fails(self):
        async def failing(request: ModelRequest) -> ModelResponse:
            raise RuntimeError("provider down")

        async def handler(request: ModelRequest) -> ModelResponse:
            return ModelResponse(result=[AIMessage(content="ok")])

        middleware = _middleware()
        with _patched_config(recursion_limit=500, step=470), pytest.raises(RuntimeError):
            await middleware.awrap_model_call(_request(), failing)
        with _patched_config(recursion_limit=500, step=470):
            response = await middleware.awrap_model_call(_request(), handler)

        assert is_synthetic(response.result[0])


class TestBind:
    def test_model_and_tools_only(self):
        middleware = StepBudgetMiddleware()
        middleware.bind(create_agent(model=GenericFakeChatModel(messages=iter([])), tools=[]))

        assert (middleware.steps_per_turn, middleware.setup_steps) == (2, 0)

    def test_reads_each_hook_node(self):
        middleware = StepBudgetMiddleware()
        middleware.bind(
            create_agent(
                model=GenericFakeChatModel(messages=iter([])),
                middleware=[_BeforeAgentHook(), TodoListMiddleware(), _BeforeModelHook()],
            )
        )

        assert (middleware.steps_per_turn, middleware.setup_steps) == (4, 1)

    async def test_announced_turns_are_the_tool_rounds_a_deep_agent_still_runs(self):
        rounds: list[int] = []
        announced: list[tuple[int, int]] = []

        @tool
        def ping() -> str:
            """Ping."""
            rounds.append(1)
            return "pong"

        class Recording(StepBudgetMiddleware):
            def _budget_reminder(self):
                reminder = super()._budget_reminder()
                if reminder is not None:
                    announced.append((int(re.search(r"(\d+) tool-call turns", reminder[1])[1]), len(rounds)))
                return reminder

        step_budget = Recording(warn_remaining_turns=6, finalize_remaining_turns=3)
        agent = create_deep_agent(
            model=_looping_model(),
            tools=[ping],
            middleware=[_BeforeAgentHook(), TodoListMiddleware(), _BeforeModelHook(), step_budget],
            checkpointer=InMemorySaver(),
        )
        step_budget.bind(agent)

        with pytest.raises(GraphRecursionError):
            await agent.ainvoke(
                {"messages": [HumanMessage(content="hi")]}, {"configurable": {"thread_id": "t"}, "recursion_limit": 80}
            )

        assert [turns for turns, _ in announced] == [6, 3]
        assert [len(rounds) - before for _, before in announced] == [6, 3]
