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


class TestStepBudgetMiddleware:
    middleware = StepBudgetMiddleware(warn_remaining_steps=40, finalize_remaining_steps=16)

    def test_no_reminder_far_from_limit(self):
        with _patched_config(recursion_limit=500, step=100):
            assert self.middleware._budget_reminder() is None

    def test_warning_reminder_in_warn_zone(self):
        with _patched_config(recursion_limit=500, step=470):
            reminder = self.middleware._budget_reminder()
        assert reminder is not None
        # 30 supersteps left ≈ 15 turns
        assert "15 tool-call turns" in reminder
        assert "hard-stopped" in reminder

    def test_finalize_reminder_near_limit(self):
        with _patched_config(recursion_limit=500, step=490):
            reminder = self.middleware._budget_reminder()
        assert reminder is not None
        assert "NOW" in reminder

    def test_no_reminder_without_recursion_limit_in_config(self):
        with _patched_config(recursion_limit=None, step=470):
            assert self.middleware._budget_reminder() is None

    def test_no_reminder_without_step_in_config(self):
        with _patched_config(recursion_limit=500, step=None):
            assert self.middleware._budget_reminder() is None

    async def test_wrap_model_call_appends_reminder_to_request(self):
        seen_requests = []

        async def handler(request: ModelRequest) -> ModelResponse:
            seen_requests.append(request)
            return ModelResponse(result=[AIMessage(content="ok")])

        request = _request()
        with _patched_config(recursion_limit=500, step=470):
            await self.middleware.awrap_model_call(request, handler)

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
            await self.middleware.awrap_model_call(request, handler)

        assert seen_requests[0] is request
