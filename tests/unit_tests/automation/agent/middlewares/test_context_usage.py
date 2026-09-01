from unittest.mock import AsyncMock, patch

from langchain.agents.middleware import ModelRequest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from automation.agent.events import CONTEXT_USAGE_EVENT, context_usage_payload
from automation.agent.middlewares.context_usage import ContextUsageMiddleware
from automation.agent.usage_tracking import ResolvedWindow

USAGE = {
    "input_tokens": 120_000,
    "output_tokens": 1_500,
    "total_tokens": 121_500,
    "input_token_details": {"cache_read": 100_000},
}


def _request(profile=None) -> ModelRequest:
    return ModelRequest(
        model=GenericFakeChatModel(messages=iter([]), profile=profile), messages=[HumanMessage(content="hi")]
    )


def _response(**message_kwargs) -> ModelResponse:
    return ModelResponse(result=[AIMessage(content="ok", **message_kwargs)])


def _handler(response):
    async def handler(_request):
        return response

    return handler


def _patched_dispatch(**mock_kwargs):
    return patch("automation.agent.middlewares.context_usage.adispatch_custom_event", new=AsyncMock(**mock_kwargs))


async def test_emits_once_per_model_call_with_the_built_payload():
    response = _response(usage_metadata=USAGE, response_metadata={"model_name": "claude-sonnet-4-6"})

    with _patched_dispatch() as dispatch:
        result = await ContextUsageMiddleware().awrap_model_call(
            _request(profile={"max_input_tokens": 1_000_000}), _handler(response)
        )

    assert result is response
    dispatch.assert_awaited_once_with(
        CONTEXT_USAGE_EVENT,
        context_usage_payload(model="claude-sonnet-4-6", usage=USAGE, window=ResolvedWindow(1_000_000, "profile")),
    )


async def test_emits_nothing_when_usage_metadata_is_missing():
    response = _response(response_metadata={"model_name": "claude-sonnet-4-6"})

    with _patched_dispatch() as dispatch:
        await ContextUsageMiddleware().awrap_model_call(_request(), _handler(response))

    dispatch.assert_not_awaited()


async def test_emits_nothing_when_model_name_is_missing():
    response = _response(usage_metadata=USAGE)

    with _patched_dispatch() as dispatch:
        await ContextUsageMiddleware().awrap_model_call(_request(), _handler(response))

    dispatch.assert_not_awaited()


async def test_a_dispatch_failure_never_fails_the_model_call():
    response = _response(usage_metadata=USAGE, response_metadata={"model_name": "claude-sonnet-4-6"})

    with _patched_dispatch(side_effect=RuntimeError("no parent run")):
        result = await ContextUsageMiddleware().awrap_model_call(_request(), _handler(response))

    assert result is response


async def test_the_anticipated_transport_failure_stays_a_warning():
    """A missing parent run id is an outage-shaped condition, not a bug — it must not
    mint a Sentry error event per model call."""
    response = _response(usage_metadata=USAGE, response_metadata={"model_name": "claude-sonnet-4-6"})

    with (
        _patched_dispatch(side_effect=RuntimeError("no parent run")),
        patch("automation.agent.middlewares.context_usage.logger") as log,
    ):
        await ContextUsageMiddleware().awrap_model_call(_request(), _handler(response))

    log.warning.assert_called_once()
    log.exception.assert_not_called()


async def test_an_unexpected_failure_logs_at_error_without_failing_the_call():
    """A bug anywhere in the derivation chain must be visible to prod monitoring
    (``logger.warning`` never becomes a Sentry event) while staying non-fatal."""
    response = _response(usage_metadata=USAGE, response_metadata={"model_name": "claude-sonnet-4-6"})

    with (
        _patched_dispatch(side_effect=ValueError("builder bug")),
        patch("automation.agent.middlewares.context_usage.logger") as log,
    ):
        result = await ContextUsageMiddleware().awrap_model_call(_request(), _handler(response))

    assert result is response
    log.exception.assert_called_once()
    log.warning.assert_not_called()


async def test_a_doubled_streamed_name_still_divides_by_the_right_window():
    """A gateway with no source-level dedupe streams a doubled name; the ratio the meter draws
    is the window looked up from it. ``_request()`` carries no profile on purpose — a profile
    resolves the window off the model instance and never reads the name at all.
    """
    response = _response(usage_metadata=USAGE, response_metadata={"model_name": "anthropic/claude-sonnet-4.6" * 2})

    with _patched_dispatch() as dispatch:
        await ContextUsageMiddleware().awrap_model_call(_request(), _handler(response))

    payload = dispatch.await_args.args[1]
    assert payload["model"] == "anthropic/claude-sonnet-4.6"
    assert payload["window_tokens"] == 1_000_000


async def test_an_unresolved_window_still_emits_the_count():
    response = _response(usage_metadata=USAGE, response_metadata={"model_name": "model-nobody-knows"})

    with _patched_dispatch() as dispatch:
        await ContextUsageMiddleware().awrap_model_call(_request(), _handler(response))

    payload = dispatch.await_args.args[1]
    assert payload["used_tokens"] == 121_500
    assert payload["window_tokens"] is None
