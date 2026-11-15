import logging
from unittest.mock import ANY, AsyncMock, Mock

from django.core.cache import cache

import httpx
import pytest
from pydantic import ValidationError

from core.sandbox.capabilities import (
    CAPABILITIES_CACHE_KEY,
    NO_BROWSER_MESSAGE,
    UNREACHABLE_CACHE_TTL,
    SandboxCapabilityProbe,
)
from core.sandbox.schemas import SandboxCapabilities


def _client(result=None, error: Exception | None = None):
    client = Mock()
    client.get_capabilities = AsyncMock(return_value=result, side_effect=error)
    return client


async def test_aget_caches_the_answer_across_calls():
    client = _client(SandboxCapabilities(browser=True))

    first = await SandboxCapabilityProbe().aget(client)
    second = await SandboxCapabilityProbe().aget(client)

    assert first.browser is True
    assert second.browser is True
    client.get_capabilities.assert_awaited_once()


async def test_aget_reports_no_browser_on_an_old_service_and_logs_once_per_refresh(caplog):
    client = _client(SandboxCapabilities(browser=False))

    with caplog.at_level(logging.ERROR, logger="daiv.sandbox"):
        assert (await SandboxCapabilityProbe().aget(client)).browser is False
        assert (await SandboxCapabilityProbe().aget(client)).browser is False

    assert [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR] == [NO_BROWSER_MESSAGE]


async def test_aget_answers_none_on_a_transport_error_and_keeps_that_answer_for_a_shorter_period(caplog, monkeypatch):
    aset = AsyncMock(wraps=cache.aset)
    monkeypatch.setattr("core.sandbox.capabilities.cache.aset", aset)
    client = _client(error=httpx.ConnectError("refused"))

    with caplog.at_level(logging.ERROR, logger="daiv.sandbox"):
        assert await SandboxCapabilityProbe().aget(client) is None
        client.get_capabilities.side_effect = None
        client.get_capabilities.return_value = SandboxCapabilities(browser=True)
        assert await SandboxCapabilityProbe().aget(client) is None

    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1
    client.get_capabilities.assert_awaited_once()
    aset.assert_awaited_once_with(CAPABILITIES_CACHE_KEY, ANY, UNREACHABLE_CACHE_TTL)


async def test_aget_asks_again_when_the_cached_value_does_not_parse():
    await cache.aset(CAPABILITIES_CACHE_KEY, {"browser": "maybe"})
    client = _client(SandboxCapabilities(browser=True))

    assert (await SandboxCapabilityProbe().aget(client)).browser is True
    client.get_capabilities.assert_awaited_once()


async def test_aget_asks_the_service_when_the_cache_fails(monkeypatch):
    monkeypatch.setattr("core.sandbox.capabilities.cache.aget", AsyncMock(side_effect=ConnectionError("redis down")))
    monkeypatch.setattr("core.sandbox.capabilities.cache.aset", AsyncMock(side_effect=ConnectionError("redis down")))
    client = _client(SandboxCapabilities(browser=True))

    assert (await SandboxCapabilityProbe().aget(client)).browser is True
    client.get_capabilities.assert_awaited_once()


async def test_aget_opens_its_own_client_when_none_is_given(monkeypatch):
    opened = _client(SandboxCapabilities(browser=True))

    class FakeClient:
        async def __aenter__(self):
            return opened

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr("core.sandbox.capabilities.DAIVSandboxClient", FakeClient)

    assert (await SandboxCapabilityProbe().aget()).browser is True
    opened.get_capabilities.assert_awaited_once()


@pytest.mark.parametrize(
    "error", [ValueError("Expecting value"), ValidationError.from_exception_data("SandboxCapabilities", [])]
)
async def test_aget_answers_none_on_a_malformed_answer(error):
    assert await SandboxCapabilityProbe().aget(_client(error=error)) is None
