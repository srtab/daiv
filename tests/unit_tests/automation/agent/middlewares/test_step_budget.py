from unittest.mock import patch

from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from automation.agent.middlewares.step_budget import StepBudgetMiddleware


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


def _middleware(baseline: int | None = 0, heartbeat_every_calls: int | None = None) -> StepBudgetMiddleware:
    """A fresh middleware whose run started at ``baseline`` (default 0, i.e. a fresh thread).

    ``None`` leaves the baseline uncaptured so the next ``_budget_reminder`` call records it,
    mirroring the lazy capture on the first model call of a run.
    """
    middleware = StepBudgetMiddleware(
        warn_remaining_steps=40, finalize_remaining_steps=16, heartbeat_every_calls=heartbeat_every_calls
    )
    middleware._baseline_step = baseline
    return middleware


class TestStepBudgetMiddleware:
    def test_no_reminder_far_from_limit(self):
        with _patched_config(recursion_limit=500, step=100):
            assert _middleware()._budget_reminder() is None

    def test_warning_reminder_in_warn_zone(self):
        with _patched_config(recursion_limit=500, step=470):
            reminder = _middleware()._budget_reminder()
        assert reminder is not None
        # 30 supersteps left ≈ 15 turns
        assert "15 tool-call turns" in reminder
        assert "hard-stopped" in reminder

    def test_finalize_reminder_near_limit(self):
        with _patched_config(recursion_limit=500, step=490):
            reminder = _middleware()._budget_reminder()
        assert reminder is not None
        assert "NOW" in reminder

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
        assert "15 tool-call turns" in reminder

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

    async def test_wrap_model_call_appends_reminder_to_request(self):
        seen_requests = []

        async def handler(request: ModelRequest) -> ModelResponse:
            seen_requests.append(request)
            return ModelResponse(result=[AIMessage(content="ok")])

        request = _request()
        with _patched_config(recursion_limit=500, step=470):
            await _middleware().awrap_model_call(request, handler)

        assert len(seen_requests[0].messages) == 2
        assert "tool-call turns" in seen_requests[0].messages[-1].content
        # The reminder is ephemeral: the original request is untouched.
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


def _recording_handler():
    seen: list[ModelRequest] = []

    async def handler(request: ModelRequest) -> ModelResponse:
        seen.append(request)
        return ModelResponse(result=[AIMessage(content="ok")])

    handler.seen = seen
    return handler


def _read_file_history(*paths: str) -> list:
    messages: list = [HumanMessage(content="audit")]
    for i, path in enumerate(paths):
        messages.append(
            AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"file_path": path}, "id": f"c{i}"}])
        )
    return messages


def _request_with_history(n_ai_messages: int) -> ModelRequest:
    """Build a request whose history already contains ``n_ai_messages`` AIMessages.

    With the stateless per-run count, ``calls = n_ai_messages + 1``; the heartbeat fires when
    ``calls % heartbeat_every_calls == 0``, i.e. when ``n_ai_messages + 1`` is a multiple of N.
    """
    messages: list = [HumanMessage(content="task")]
    for i in range(n_ai_messages):
        messages.append(AIMessage(content=f"step {i}"))
    return ModelRequest(model=GenericFakeChatModel(messages=iter([])), messages=messages)


class TestHeartbeat:
    async def test_disabled_by_default_never_fires(self):
        middleware = _middleware()
        handler = _recording_handler()
        with _patched_config(recursion_limit=500, step=10):
            for _ in range(30):
                await middleware.awrap_model_call(_request(), handler)
        assert all(len(request.messages) == 1 for request in handler.seen)

    async def test_fires_on_every_nth_model_call(self):
        # N=5: heartbeat fires when calls (= prior AIMessages + 1) is a multiple of 5.
        # call 5  → request with 4 prior AIMessages → fires, text says "5 model calls"
        # call 4  → request with 3 prior AIMessages → silent
        # call 10 → request with 9 prior AIMessages → fires
        middleware = _middleware(heartbeat_every_calls=5)
        handler = _recording_handler()
        with _patched_config(recursion_limit=500, step=10):
            # call 4 (3 prior AIs) — should NOT fire
            await middleware.awrap_model_call(_request_with_history(3), handler)
            # call 5 (4 prior AIs) — SHOULD fire
            await middleware.awrap_model_call(_request_with_history(4), handler)
            # call 9 (8 prior AIs) — should NOT fire
            await middleware.awrap_model_call(_request_with_history(8), handler)
            # call 10 (9 prior AIs) — SHOULD fire
            await middleware.awrap_model_call(_request_with_history(9), handler)
        assert len(handler.seen[0].messages) == 4  # 1 Human + 3 AIs, no reminder
        assert len(handler.seen[1].messages) == 6  # 1 Human + 4 AIs + reminder
        assert len(handler.seen[2].messages) == 9  # 1 Human + 8 AIs, no reminder
        assert len(handler.seen[3].messages) == 11  # 1 Human + 9 AIs + reminder
        assert "Progress check" in handler.seen[1].messages[-1].content
        assert "5 model calls" in handler.seen[1].messages[-1].content

    async def test_heartbeat_count_does_not_leak_across_instance_invocations(self):
        """Regression: the per-run count must NOT accumulate across separate awrap_model_call
        invocations on the same middleware instance.

        A subagent compiled once and invoked multiple times shares the same middleware instance.
        Each invocation's request history resets the count — a second call whose request carries
        few prior AIMessages must be decided independently of how many calls the instance has seen.
        """
        middleware = _middleware(heartbeat_every_calls=5)
        handler = _recording_handler()
        with _patched_config(recursion_limit=500, step=10):
            # First invocation: simulate a run that reaches call 5 (4 prior AIs) → fires.
            await middleware.awrap_model_call(_request_with_history(4), handler)
            # Second invocation on the SAME instance: fresh run with only 1 prior AI (call 2).
            # Under the old instance-counter logic this would see _model_calls == 2 and might
            # count toward a future threshold accumulated from the first run. Under the new
            # per-run logic, calls == 2, which is NOT a multiple of 5, so it must NOT fire.
            await middleware.awrap_model_call(_request_with_history(1), handler)
        assert "Progress check" in handler.seen[0].messages[-1].content  # first run fired
        assert len(handler.seen[1].messages) == 2  # second run: no reminder (1 Human + 1 AI)

    async def test_heartbeat_includes_reread_stats(self):
        middleware = _middleware(heartbeat_every_calls=1)
        handler = _recording_handler()
        request = ModelRequest(
            model=GenericFakeChatModel(messages=iter([])),
            messages=_read_file_history(
                "/a/callbacks.py", "/a/callbacks.py", "/a/callbacks.py", "/a/callbacks.py", "/a/other.py"
            ),
        )
        with _patched_config(recursion_limit=500, step=10):
            await middleware.awrap_model_call(request, handler)
        reminder = handler.seen[0].messages[-1].content
        assert "2 distinct file(s)" in reminder
        assert "callbacks.py (4x)" in reminder
        assert "other.py" not in reminder  # below the re-read floor

    async def test_budget_reminder_takes_precedence_over_heartbeat(self):
        middleware = _middleware(heartbeat_every_calls=1)
        handler = _recording_handler()
        with _patched_config(recursion_limit=500, step=470):  # warn zone
            await middleware.awrap_model_call(_request(), handler)
        reminder = handler.seen[0].messages[-1].content
        assert "tool-call turns" in reminder
        assert "Progress check" not in reminder

    def test_invalid_cadence_rejected(self):
        import pytest

        with pytest.raises(ValueError):
            StepBudgetMiddleware(heartbeat_every_calls=0)
