import threading
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from django.db import DatabaseError
from django.utils import timezone

import httpx2
import openai
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from automation.agent.provider_errors import provider_failure_of
from automation.agent.provider_health import ProviderHealthCallback, mark_provider_failure
from core.models import PROVIDER_FAILURE_TTL, Provider, ProviderFailureKind
from core.provider_failures import ProviderFailure

K = ProviderFailureKind


class RaisingChatModel(BaseChatModel):
    error: Exception

    @property
    def _llm_type(self) -> str:
        return "raising"

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Any:
        raise self.error


def openrouter_status(status: int, body: dict) -> openai.APIStatusError:
    request = httpx2.Request("POST", "https://openrouter.test/v1/chat/completions")
    client = openai.OpenAI(api_key="k", base_url="https://openrouter.test/v1")
    return client._make_status_error_from_response(httpx2.Response(status, json=body, request=request))


def openrouter_402() -> openai.APIStatusError:
    return openrouter_status(
        402, {"error": {"code": 402, "message": "x", "metadata": {"limit_source": "openrouter_credits"}}}
    )


def failing_call(model_name: str, error: Exception) -> Exception:
    model = RaisingChatModel(error=error, callbacks=[ProviderHealthCallback("openrouter", model_name)])
    with pytest.raises(type(error)) as excinfo:
        model.invoke("hi")
    return excinfo.value


def succeeding_call(model_name: str) -> None:
    FakeListChatModel(responses=["ok"], callbacks=[ProviderHealthCallback("openrouter", model_name)]).invoke("hi")


def openrouter_row() -> Provider:
    return Provider.objects.get(slug="openrouter")


@pytest.mark.django_db
def test_a_credits_error_is_tagged_and_marks_the_provider():
    error = openrouter_402()

    raised = failing_call("m-big", error)

    assert raised is error
    assert provider_failure_of(error) == ProviderFailure(K.OUT_OF_CREDITS, "openrouter")
    row = openrouter_row()
    assert row.failure_kind == K.OUT_OF_CREDITS
    assert row.failure_model == "m-big"
    assert row.failing_since is not None
    assert row.last_failed_at == row.failing_since


@pytest.mark.django_db
def test_a_repeat_keeps_failing_since_and_bumps_last_failed_at():
    first = timezone.now() - timedelta(hours=1)
    mark_provider_failure("openrouter", K.OUT_OF_CREDITS, "m-big", now=first)

    failing_call("m-big", openrouter_402())

    row = openrouter_row()
    assert row.failing_since == first
    assert row.last_failed_at > first


@pytest.mark.django_db
def test_a_failure_after_the_mark_expired_restarts_failing_since():
    expired = timezone.now() - PROVIDER_FAILURE_TTL - timedelta(hours=1)
    mark_provider_failure("openrouter", K.OUT_OF_CREDITS, "m-big", now=expired)

    failing_call("m-big", openrouter_402())

    row = openrouter_row()
    assert row.failing_since > expired
    assert row.failing_since == row.last_failed_at


@pytest.mark.django_db
def test_a_rate_limit_is_tagged_but_marks_nothing():
    error = openrouter_status(429, {"error": {"code": 429, "message": "slow down"}})

    failing_call("m-big", error)

    assert provider_failure_of(error) == ProviderFailure(K.RATE_LIMITED, "openrouter")
    row = openrouter_row()
    assert row.failure_kind == ""
    assert row.last_failed_at is None


@pytest.mark.django_db
def test_success_on_another_model_keeps_the_credits_mark():
    failing_call("m-big", openrouter_402())

    succeeding_call("m-cheap")

    row = openrouter_row()
    assert row.failure_kind == K.OUT_OF_CREDITS
    assert row.failure_model == "m-big"


@pytest.mark.django_db
def test_success_on_the_failing_model_clears_the_credits_mark():
    failing_call("m-big", openrouter_402())
    assert openrouter_row().failure_kind == K.OUT_OF_CREDITS

    succeeding_call("m-big")

    row = openrouter_row()
    assert (row.failure_kind, row.failure_model, row.failing_since, row.last_failed_at) == ("", "", None, None)
    assert Provider.get_cached_by_slug()["openrouter"].failure_kind == ""


@pytest.mark.django_db
def test_any_success_clears_an_auth_mark():
    failing_call("m-big", openrouter_status(401, {"error": {"code": 401, "message": "No auth credentials found"}}))
    assert openrouter_row().failure_kind == K.AUTH_FAILED

    succeeding_call("m-cheap")

    assert openrouter_row().failure_kind == ""


@pytest.mark.django_db
def test_a_db_failure_while_marking_keeps_the_original_error(caplog):
    error = openrouter_402()

    with patch.object(Provider.objects, "filter", side_effect=DatabaseError("database is down")):
        raised = failing_call("m-big", error)

    assert raised is error
    assert provider_failure_of(error) == ProviderFailure(K.OUT_OF_CREDITS, "openrouter")
    assert any(record.exc_info and record.exc_info[1].__class__ is DatabaseError for record in caplog.records)


async def test_an_async_call_is_tagged_before_it_raises():
    error = openrouter_402()
    marked_on: list[int] = []
    model = RaisingChatModel(error=error, callbacks=[ProviderHealthCallback("openrouter", "m-big")])

    with (
        patch(
            "automation.agent.provider_health.mark_provider_failure",
            side_effect=lambda *args, **kwargs: marked_on.append(threading.get_ident()),
        ) as mark,
        pytest.raises(openai.APIStatusError) as excinfo,
    ):
        await model.ainvoke("hi")

    assert excinfo.value is error
    assert provider_failure_of(error) == ProviderFailure(K.OUT_OF_CREDITS, "openrouter")
    mark.assert_called_once_with("openrouter", K.OUT_OF_CREDITS, "m-big")
    [marking_thread] = marked_on
    assert marking_thread != threading.get_ident()
