import asyncio
import threading
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from django.db import DatabaseError
from django.utils import timezone

import httpx2
import openai
import pytest
from asgiref.sync import sync_to_async
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from automation.agent import provider_health
from automation.agent.provider_errors import provider_failure_of
from automation.agent.provider_health import ProviderHealthCallback, clear_provider_failure, mark_provider_failure
from core.models import PROVIDER_FAILURE_TTL, Provider, ProviderFailureKind, ProviderType
from core.provider_failures import ProviderFailure

if TYPE_CHECKING:
    from collections.abc import Callable

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


def on_threads(job: Callable[..., None], threads: list[int]) -> Callable[..., None]:
    def run(*args: Any, **kwargs: Any) -> None:
        threads.append(threading.get_ident())
        job(*args, **kwargs)

    return run


async def flush_health_jobs() -> None:
    await asyncio.wrap_future(provider_health._executor.submit(lambda: None))


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
def test_success_keeps_a_mark_another_worker_moved_to_a_different_model():
    failing_call("m-big", openrouter_402())
    assert Provider.get_cached_by_slug()["openrouter"].failure_model == "m-big"
    Provider.objects.filter(slug="openrouter").update(failure_model="m-other")

    succeeding_call("m-big")

    row = openrouter_row()
    assert (row.failure_kind, row.failure_model) == (K.OUT_OF_CREDITS, "m-other")


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


@pytest.mark.django_db(transaction=True)
async def test_an_async_call_is_tagged_before_it_raises():
    await Provider.objects.acreate(slug="health-async", display_name="Health", provider_type=ProviderType.OPENROUTER)
    error = openrouter_402()
    model = RaisingChatModel(error=error, callbacks=[ProviderHealthCallback("health-async", "m-big")])
    job_threads: list[int] = []

    with patch(
        "automation.agent.provider_health.mark_provider_failure", on_threads(mark_provider_failure, job_threads)
    ):
        with pytest.raises(openai.APIStatusError) as excinfo:
            await model.ainvoke("hi")
        assert excinfo.value is error
        assert provider_failure_of(error) == ProviderFailure(K.OUT_OF_CREDITS, "health-async")
        await flush_health_jobs()

    row = await Provider.objects.aget(slug="health-async")
    assert (row.failure_kind, row.failure_model) == (K.OUT_OF_CREDITS, "m-big")
    [job_thread] = job_threads
    assert job_thread != threading.get_ident()


@pytest.mark.django_db(transaction=True)
async def test_an_async_success_clears_the_mark_off_the_event_loop():
    await Provider.objects.acreate(slug="health-async", display_name="Health", provider_type=ProviderType.OPENROUTER)
    await sync_to_async(mark_provider_failure)("health-async", K.OUT_OF_CREDITS, "m-big")
    model = FakeListChatModel(responses=["ok"], callbacks=[ProviderHealthCallback("health-async", "m-big")])
    job_threads: list[int] = []

    with patch(
        "automation.agent.provider_health.clear_provider_failure", on_threads(clear_provider_failure, job_threads)
    ):
        await model.ainvoke("hi")
        await flush_health_jobs()

    row = await Provider.objects.aget(slug="health-async")
    assert row.failure_kind == ""
    [job_thread] = job_threads
    assert job_thread != threading.get_ident()
