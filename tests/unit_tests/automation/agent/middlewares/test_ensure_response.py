from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from automation.agent.middlewares.ensure_response import (
    EMPTY_RESPONSE_NUDGE,
    MAX_EMPTY_RESPONSE_RETRIES,
    ensure_non_empty_response,
)


def _request(messages: list | None = None) -> ModelRequest:
    return ModelRequest(
        model=GenericFakeChatModel(messages=iter([])), messages=messages or [HumanMessage(content="hi")]
    )


def _handler(responses: list[AIMessage]):
    """Build a fake handler that pops canned responses and records the requests it receives."""
    seen_requests: list[ModelRequest] = []

    async def handler(request: ModelRequest) -> ModelResponse:
        seen_requests.append(request)
        return ModelResponse(result=[responses[min(len(seen_requests), len(responses)) - 1]])

    return handler, seen_requests


class TestEnsureNonEmptyResponse:
    async def test_returns_response_with_content_without_retrying(self):
        handler, seen = _handler([AIMessage(content="Hello, I can help with that.")])
        response = await ensure_non_empty_response.awrap_model_call(_request(), handler)

        assert len(seen) == 1
        assert response.result[-1].text() == "Hello, I can help with that."

    async def test_returns_response_with_tool_calls_without_retrying(self):
        msg = AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"path": "foo.py"}, "id": "tc-1"}])
        handler, seen = _handler([msg])
        response = await ensure_non_empty_response.awrap_model_call(_request(), handler)

        assert len(seen) == 1
        assert response.result[-1].tool_calls

    async def test_retries_empty_response_with_nudge(self):
        handler, seen = _handler([AIMessage(content=""), AIMessage(content="Recovered.")])
        response = await ensure_non_empty_response.awrap_model_call(_request(), handler)

        assert len(seen) == 2
        assert response.result[-1].text() == "Recovered."
        # The retry request carries the nudge appended to the original messages.
        retry_messages = seen[1].messages
        assert retry_messages[-1].content == EMPTY_RESPONSE_NUDGE
        assert len(retry_messages) == len(seen[0].messages) + 1

    async def test_gives_up_after_max_retries(self):
        empty = AIMessage(content="")
        handler, seen = _handler([empty] * (MAX_EMPTY_RESPONSE_RETRIES + 1))
        response = await ensure_non_empty_response.awrap_model_call(_request(), handler)

        assert len(seen) == MAX_EMPTY_RESPONSE_RETRIES + 1
        # The empty response is returned as-is so the agent loop ends instead of spinning.
        assert not response.result[-1].text()
        assert not response.result[-1].tool_calls

    async def test_does_not_mutate_original_request_messages(self):
        original_messages = [HumanMessage(content="hi")]
        request = _request(messages=original_messages)
        handler, _ = _handler([AIMessage(content=""), AIMessage(content="ok")])
        await ensure_non_empty_response.awrap_model_call(request, handler)

        assert request.messages == original_messages
