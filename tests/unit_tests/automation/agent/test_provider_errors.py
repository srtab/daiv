import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx2
import openai
import pytest
from anthropic._error_classes import make_status_error as anthropic_make_status_error
from anthropic.types.shared.enforced_spend_limit_reached_error import EnforcedSpendLimitReachedError
from google.genai.errors import ClientError, ServerError
from langchain_google_genai.chat_models import ChatGoogleGenerativeAIError

from automation.agent.provider_errors import classify_provider_error, provider_failure_of, tag_provider_failure
from core.models import ProviderFailureKind
from core.provider_failures import ProviderFailure

if TYPE_CHECKING:
    import anthropic

REQ = httpx2.Request("POST", "https://provider.test/v1/chat")
K = ProviderFailureKind


def _response(status: int, body: dict) -> httpx2.Response:
    return httpx2.Response(status, json=body, request=REQ)


def openai_status(status: int, body: dict) -> openai.APIStatusError:
    client = openai.OpenAI(api_key="k", base_url="https://provider.test/v1")
    return client._make_status_error_from_response(_response(status, body))


def anthropic_status(status: int, body: dict) -> anthropic.APIStatusError:
    response = _response(status, body)
    return anthropic_make_status_error(f"Error code: {status}", body=body, response=response)


def gemini(cause: Exception) -> ChatGoogleGenerativeAIError:
    try:
        raise ChatGoogleGenerativeAIError("Error calling model") from cause
    except ChatGoogleGenerativeAIError as wrapped:
        return wrapped


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            openai_status(
                402, {"error": {"code": 402, "message": "x", "metadata": {"limit_source": "openrouter_credits"}}}
            ),
            K.OUT_OF_CREDITS,
        ),
        (openai_status(402, {"error": {"code": 402, "message": "Insufficient credits"}}), K.OUT_OF_CREDITS),
        (
            openai_status(
                402, {"error": {"code": 402, "message": "x", "metadata": {"limit_source": "openrouter_key_limit"}}}
            ),
            K.SPEND_LIMIT_REACHED,
        ),
        (
            openai_status(
                402, {"error": {"code": 402, "message": "x", "metadata": {"limit_source": "some_future_source"}}}
            ),
            None,
        ),
        (
            openai_status(
                403,
                {"error": {"code": 403, "message": "x", "metadata": {"limit_source": "openrouter_in_flight_budget"}}},
            ),
            K.RATE_LIMITED,
        ),
        (
            openai_status(
                402,
                {"error": {"code": 402, "message": "x", "metadata": {"limit_source": "openrouter_in_flight_budget"}}},
            ),
            K.RATE_LIMITED,
        ),
        (
            openai_status(
                403,
                {
                    "error": {
                        "code": 403,
                        "message": "Budget limit exceeded (weekly limit).",
                        "metadata": {"limit_source": "openrouter_key_limit"},
                    }
                },
            ),
            K.SPEND_LIMIT_REACHED,
        ),
        (
            openai_status(
                403,
                {
                    "error": {
                        "code": 403,
                        "message": "Request blocked: prompt injection patterns detected",
                        "metadata": {"patterns": ["x"]},
                    }
                },
            ),
            None,
        ),
        (openai_status(429, {"error": {"code": "insufficient_quota", "message": "x"}}), K.OUT_OF_CREDITS),
        (openai_status(429, {"error": {"code": "rate_limit_exceeded", "message": "x"}}), K.RATE_LIMITED),
        (openai_status(401, {"error": {"message": "bad key"}}), K.AUTH_FAILED),
        (openai_status(500, {"error": {"message": "x"}}), K.PROVIDER_UNAVAILABLE),
        (openai.APIConnectionError(request=REQ), K.PROVIDER_UNAVAILABLE),
        (
            anthropic_status(
                400,
                {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "Your credit balance is too low to access the Anthropic API.",
                    },
                },
            ),
            K.OUT_OF_CREDITS,
        ),
        (
            anthropic_status(
                400,
                {"type": "error", "error": {"type": "invalid_request_error", "message": "messages: field required"}},
            ),
            None,
        ),
        (
            anthropic_status(402, {"type": "error", "error": {"type": "billing_error", "message": "x"}}),
            K.OUT_OF_CREDITS,
        ),
        (
            anthropic_status(
                429,
                {
                    "type": "error",
                    "error": {
                        "type": "rate_limit_error",
                        "message": "x",
                        "details": {"error_code": "enforced_spend_limit_reached"},
                    },
                },
            ),
            K.SPEND_LIMIT_REACHED,
        ),
        (
            anthropic_status(529, {"type": "error", "error": {"type": "overloaded_error", "message": "x"}}),
            K.PROVIDER_UNAVAILABLE,
        ),
        (
            anthropic_status(401, {"type": "error", "error": {"type": "authentication_error", "message": "x"}}),
            K.AUTH_FAILED,
        ),
        (
            gemini(ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "x"}})),
            K.RATE_LIMITED,
        ),
        (
            gemini(
                ClientError(
                    400,
                    {
                        "error": {
                            "code": 400,
                            "status": "INVALID_ARGUMENT",
                            "message": "API key not valid. Please pass a valid API key.",
                        }
                    },
                )
            ),
            K.AUTH_FAILED,
        ),
        (
            gemini(ServerError(503, {"error": {"code": 503, "status": "UNAVAILABLE", "message": "x"}})),
            K.PROVIDER_UNAVAILABLE,
        ),
        (ValueError("boom"), None),
    ],
)
def test_classify_provider_error(exc, expected):
    assert classify_provider_error(exc) == expected


def test_anthropic_spend_limit_row_uses_the_sdk_class():
    exc = anthropic_status(
        429,
        {
            "type": "error",
            "error": {
                "type": "rate_limit_error",
                "message": "x",
                "details": {"error_code": "enforced_spend_limit_reached"},
            },
        },
    )
    assert isinstance(exc, EnforcedSpendLimitReachedError)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (openai_status(402, {"error": {"code": 402, "message": "Insufficient credits"}}), K.OUT_OF_CREDITS),
        (ValueError("x"), None),
    ],
)
def test_classifying_a_non_google_error_never_imports_google_genai(exc, expected):
    with patch.dict(sys.modules, {"google.genai": None}):
        assert classify_provider_error(exc) == expected


def test_classify_finds_the_provider_error_behind_a_wrapper():
    cause = openai_status(402, {"error": {"code": 402, "message": "x"}})
    try:
        raise RuntimeError("wrapped") from cause
    except RuntimeError as wrapper:
        assert classify_provider_error(wrapper) == K.OUT_OF_CREDITS


def test_provider_failure_of_reads_the_tag_through_the_cause_chain():
    failure = ProviderFailure(kind=K.OUT_OF_CREDITS, provider_slug="openrouter")
    inner = ValueError("inner")
    tag_provider_failure(inner, failure)
    try:
        raise RuntimeError("outer") from inner
    except RuntimeError as outer:
        assert provider_failure_of(outer) is failure
    assert provider_failure_of(None) is None
    assert provider_failure_of(ValueError("untagged")) is None


def test_provider_failure_of_survives_a_cause_cycle():
    a, b = ValueError("a"), ValueError("b")
    a.__cause__, b.__cause__ = b, a
    assert provider_failure_of(a) is None
    assert classify_provider_error(a) is None


def test_tagging_an_exception_that_refuses_attributes_does_not_raise():
    class FrozenError(Exception):
        def __setattr__(self, name, value):
            raise AttributeError(name)

    exc = FrozenError()
    tag_provider_failure(exc, ProviderFailure(kind=K.AUTH_FAILED, provider_slug="openai"))
    assert provider_failure_of(exc) is None


def test_classify_follows_an_implicit_context_link():
    try:
        try:
            raise openai_status(402, {"error": {"code": 402, "message": "x"}})
        except openai.APIStatusError:
            raise RuntimeError("during handling")  # noqa: B904
    except RuntimeError as exc:
        assert exc.__cause__ is None
        assert classify_provider_error(exc) == K.OUT_OF_CREDITS


def test_a_suppressed_context_is_not_followed():
    failure = ProviderFailure(kind=K.OUT_OF_CREDITS, provider_slug="openrouter")
    try:
        try:
            raise openai_status(402, {"error": {"code": 402, "message": "x"}})
        except openai.APIStatusError as inner:
            tag_provider_failure(inner, failure)
            raise RuntimeError("replaced") from None
    except RuntimeError as exc:
        assert classify_provider_error(exc) is None
        assert provider_failure_of(exc) is None


def test_tagging_swallows_type_error():
    class PickyError(Exception):
        def __setattr__(self, name, value):
            raise TypeError(name)

    tag_provider_failure(PickyError(), ProviderFailure(kind=K.AUTH_FAILED, provider_slug="openai"))
