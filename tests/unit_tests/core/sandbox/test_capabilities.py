import logging
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import ValidationError

from core.sandbox.capabilities import NO_BROWSER_MESSAGE, SandboxCapabilityProbe
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


async def test_aget_reports_no_browser_on_a_transport_error(caplog):
    client = _client(error=httpx.ConnectError("refused"))

    with caplog.at_level(logging.ERROR, logger="daiv.sandbox"):
        capabilities = await SandboxCapabilityProbe().aget(client)

    assert capabilities.browser is False
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


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
async def test_aget_reports_no_browser_on_a_malformed_answer(error):
    assert (await SandboxCapabilityProbe().aget(_client(error=error))).browser is False
