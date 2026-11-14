import asyncio
import hashlib
import json
import logging
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from stagehand import get_extension_archive
from stagehand.cdp_client import CDPConnectionClosedError
from stagehand_middleware.facade import StagehandFacadeInputError

from automation.agent.workspace import browser as browser_module
from automation.agent.workspace.browser import BrowserError, SandboxBrowser
from core.sandbox.schemas import BrowserStartResponse, BrowserStopResponse
from tests.unit_tests.conftest import FakeSandboxClient, acquired_session, sandbox_spec

START_FAILED = "error: The browser could not start. Try again; if it fails again, continue without it."
UNAVAILABLE = "error: The browser is unavailable for this run."


class FakeStagehandBrowser:
    def __init__(self) -> None:
        self.connected = True
        self.disconnect = AsyncMock()


class FakeTunnel:
    instances: list = []

    def __init__(self, client, session_id) -> None:
        self.session_id = session_id
        self.http_url = "http://127.0.0.1:40001"
        self.aclose = AsyncMock()
        FakeTunnel.instances.append(self)

    async def aopen(self):
        return self


@pytest.fixture
def stagehand_stubs():
    """Stub the tunnel, Stagehand's connect and create, and the facade; record what each got."""
    FakeTunnel.instances = []
    stubs = SimpleNamespace(created=[], facades=[], connects=[])

    async def connect(**kwargs):
        stubs.connects.append(kwargs)
        return FakeStagehandBrowser()

    async def create(**kwargs):
        stagehand = SimpleNamespace(browser=kwargs["browser"], kwargs=kwargs)
        stubs.created.append(stagehand)
        return stagehand

    def facade(stagehand, **kwargs):
        built = MagicMock(stagehand=stagehand, kwargs=kwargs)
        stubs.facades.append(built)
        return built

    with (
        patch.object(browser_module, "BrowserTunnel", FakeTunnel),
        patch.object(browser_module.local_browser, "connect", side_effect=connect),
        patch.object(browser_module.Stagehand, "create", side_effect=create),
        patch.object(browser_module, "StagehandFacadeTools", side_effect=facade),
    ):
        yield stubs


def _browser(client: FakeSandboxClient | None = None) -> SandboxBrowser:
    client = client or FakeSandboxClient.opened()
    session = acquired_session(client, spec=sandbox_spec(browser=True))
    return SandboxBrowser(session, client, memory_bytes=2**30, cpus=1.0)


def _started(profile: str, urls: list[str] | None = None) -> BrowserStartResponse:
    return BrowserStartResponse(
        started=True,
        profile=profile,
        restored_urls=urls or [],
        devtools_port=39222,
        browser_ws_path="/devtools/browser/guid",
    )


async def test_nothing_starts_until_the_first_call(stagehand_stubs):
    browser = _browser()

    assert browser.state == "idle"
    assert browser.client.calls_to("start_browser") == []


async def test_the_first_call_starts_the_browser_once_with_the_extension(stagehand_stubs):
    browser = _browser()

    first = await browser.aensure()
    second = await browser.aensure()

    assert first is second
    assert browser.state == "ready"
    [(session_id, request, archive)] = browser.client.calls_to("start_browser")
    assert session_id == "sess-1"
    assert archive == get_extension_archive()
    assert request.extension_sha256 == hashlib.sha256(archive).hexdigest()
    assert (request.memory_bytes, request.cpus) == (2**30, 1.0)


async def test_stagehand_connects_through_the_tunnel_with_no_model(stagehand_stubs):
    browser = _browser()

    await browser.aensure()

    [connect] = stagehand_stubs.connects
    assert connect == {
        "cdp_url": "http://127.0.0.1:40001",
        "extension_cdp_url": "ws://127.0.0.1:39222/devtools/browser/guid",
    }
    [stagehand] = stagehand_stubs.created
    assert "model" not in stagehand.kwargs
    assert stagehand.kwargs["logging"] == {"level": "off"}


async def test_the_facade_writes_artifacts_to_a_run_directory(stagehand_stubs):
    browser = _browser()

    await browser.aensure()

    [facade] = stagehand_stubs.facades
    artifact_root = facade.kwargs["artifact_root"]
    assert pathlib.Path(artifact_root).is_dir()
    assert pathlib.Path(artifact_root).name.startswith("daiv-browser-")
    assert artifact_root != pathlib.Path.cwd()
    await browser.arelease()
    assert not pathlib.Path(artifact_root).exists()


@pytest.mark.parametrize(
    ("response", "note"),
    [
        pytest.param(
            _started("restored", ["http://localhost:5173/login", "http://localhost:5173/"]),
            "Browser restored from this session's profile; re-opened: http://localhost:5173/login, "
            "http://localhost:5173/.",
            id="restored-with-tabs",
        ),
        pytest.param(
            _started("restored"),
            "Browser restored from this session's profile (cookies and storage kept, no open pages).",
            id="restored-without-tabs",
        ),
        pytest.param(
            _started("reset"), "This session's browser profile was reset; the browser started empty.", id="reset"
        ),
        pytest.param(_started("empty"), None, id="empty"),
    ],
)
async def test_the_first_result_tells_the_agent_about_the_profile_once(stagehand_stubs, response, note):
    client = FakeSandboxClient.opened()
    client.browser_start_response = response
    browser = _browser(client)

    await browser.aensure()

    assert browser.take_note() == note
    assert browser.take_note() is None


async def test_release_disconnects_closes_the_tunnel_and_stops_the_browser_once(stagehand_stubs):
    browser = _browser()
    await browser.aensure()
    stagehand = stagehand_stubs.created[0]
    [tunnel] = FakeTunnel.instances

    await browser.arelease()
    await browser.arelease()

    stagehand.browser.disconnect.assert_awaited_once()
    tunnel.aclose.assert_awaited_once()
    assert browser.client.calls_to("stop_browser") == [("sess-1", False)]
    assert browser.state == "released"


async def test_release_without_stop_leaves_the_container_to_its_new_holder(stagehand_stubs):
    browser = _browser()
    await browser.aensure()

    await browser.arelease(stop=False)

    assert browser.client.calls_to("stop_browser") == []


async def test_a_browser_never_used_is_not_stopped_at_release(stagehand_stubs):
    browser = _browser()

    await browser.arelease()

    assert browser.client.calls_to("stop_browser") == []


async def test_release_logs_and_swallows_a_failed_stop(stagehand_stubs, caplog):
    client = FakeSandboxClient.opened()
    client.fail("stop_browser", status=500)
    browser = _browser(client)
    await browser.aensure()

    with caplog.at_level(logging.ERROR, logger="daiv.tools"):
        await browser.arelease()

    assert browser.state == "released"
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_a_call_after_release_is_refused(stagehand_stubs):
    browser = _browser()
    await browser.arelease()

    with pytest.raises(BrowserError, match="unavailable"):
        await browser.aensure()


async def test_a_facade_error_passes_through_and_keeps_the_browser(stagehand_stubs):
    browser = _browser()

    async def stale(_facade):
        raise StagehandFacadeInputError("Snapshot ID 1-2 is stale; take a new snapshot.")

    with pytest.raises(StagehandFacadeInputError, match="stale"):
        await browser.acall(stale)

    assert browser.state == "ready"
    assert browser.client.calls_to("stop_browser") == []


@pytest.mark.parametrize(
    ("oom", "reason", "level"),
    [(False, "the connection was lost", logging.WARNING), (True, "it ran out of memory", logging.ERROR)],
    ids=["connection", "oom"],
)
async def test_a_lost_connection_stops_the_browser_and_the_next_call_restarts_it(
    stagehand_stubs, caplog, oom, reason, level
):
    client = FakeSandboxClient.opened()
    client.browser_stop_response = BrowserStopResponse(saved_urls=[], oom_killed=oom)
    browser = _browser(client)
    await browser.aensure()
    browser.take_note()

    async def drop(_facade):
        raise CDPConnectionClosedError

    with caplog.at_level(logging.WARNING, logger="daiv.tools"), pytest.raises(BrowserError) as raised:
        await browser.acall(drop)

    assert str(raised.value) == (
        f"error: The browser stopped ({reason}). The next browser call restarts it from this session's last saved "
        "profile."
    )
    assert browser.state == "lost"
    assert client.calls_to("stop_browser") == [("sess-1", False)]
    assert [r.levelno for r in caplog.records if "stopped" in r.getMessage()] == [level]

    async def ok(_facade):
        return "Example Domain"

    assert await browser.acall(ok) == "Example Domain"
    assert len(client.calls_to("start_browser")) == 2
    assert len(stagehand_stubs.created) == 2
    assert browser.take_note() == (
        f"The browser stopped ({reason}) and restarted from this session's last saved profile; pages and state since "
        "then were lost."
    )


async def test_a_disconnected_browser_counts_as_lost_whatever_the_error(stagehand_stubs):
    browser = _browser()
    await browser.aensure()

    async def gone(_facade):
        stagehand_stubs.created[0].browser.connected = False
        raise RuntimeError("rpc failed")

    with pytest.raises(BrowserError, match="connection was lost"):
        await browser.acall(gone)


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(("fail", None), id="transport"),
        pytest.param(("fail", 503), id="non-2xx"),
        pytest.param(("hang", None), id="start-budget"),
    ],
)
async def test_a_failed_start_says_so_then_latches_unavailable(stagehand_stubs, caplog, failure, monkeypatch):
    client = FakeSandboxClient.opened()
    kind, status = failure
    if kind == "fail":
        client.fail("start_browser", status=status)
    else:
        monkeypatch.setattr(browser_module, "START_BUDGET_SECONDS", 0.01)

        async def hang(*_args, **_kwargs):
            await asyncio.sleep(10)

        client.start_browser = hang
    browser = _browser(client)

    with caplog.at_level(logging.ERROR, logger="daiv.tools"):
        with pytest.raises(BrowserError) as first:
            await browser.aensure()
        with pytest.raises(BrowserError) as second:
            await browser.aensure()
        with pytest.raises(BrowserError) as third:
            await browser.aensure()

    assert (str(first.value), str(second.value), str(third.value)) == (START_FAILED, UNAVAILABLE, UNAVAILABLE)
    assert browser.state == "unavailable"
    if kind == "fail":
        assert len(client.calls_to("start_browser")) == 2
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 2


async def test_failure_texts_carry_no_service_address_key_or_exception(stagehand_stubs):
    client = FakeSandboxClient.opened()
    client.fail("start_browser", status=None)
    browser = _browser(client)

    texts = []
    for _ in range(2):
        with pytest.raises(BrowserError) as raised:
            await browser.aensure()
        texts.append(str(raised.value))

    for text in texts:
        assert "sandbox.test" not in text
        assert "fake-key" not in text
        assert "ConnectError" not in text
        assert raised.value.__cause__ is None


async def test_release_during_a_failed_start_still_stops_the_container(stagehand_stubs):
    client = FakeSandboxClient.opened()
    client.fail("start_browser", status=None)
    browser = _browser(client)
    with pytest.raises(BrowserError):
        await browser.aensure()

    await browser.arelease()

    assert client.calls_to("stop_browser") == [("sess-1", False)]


def test_the_extension_archive_is_built_once_per_process(monkeypatch):
    build = MagicMock(return_value=b"zip")
    monkeypatch.setattr(browser_module, "get_extension_archive", build)
    browser_module._extension.cache_clear()
    try:
        first, second = browser_module._extension(), browser_module._extension()
    finally:
        browser_module._extension.cache_clear()

    assert first == second == (b"zip", hashlib.sha256(b"zip").hexdigest())
    build.assert_called_once_with()


async def test_a_transport_error_from_the_client_is_a_start_failure(stagehand_stubs):
    client = FakeSandboxClient.opened()
    browser = _browser(client)
    client.start_browser = AsyncMock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(BrowserError, match="could not start"):
        await browser.aensure()


@pytest.fixture
async def devtools_page():
    """A stand-in DevTools page endpoint answering ``Page.getNavigationHistory`` with ``state.entries``."""
    from websockets.asyncio.server import serve

    state = SimpleNamespace(entries=[], paths=[])

    async def handler(websocket):
        state.paths.append(websocket.request.path)
        async for message in websocket:
            request = json.loads(message)
            await websocket.send(json.dumps({"method": "Page.frameNavigated", "params": {}}))
            result = {"currentIndex": len(state.entries) - 1, "entries": [{"url": url} for url in state.entries]}
            await websocket.send(json.dumps({"id": request["id"], "result": result}))

    async with serve(handler, "127.0.0.1", 0) as server:
        state.http_url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        yield state


async def _browser_on_page(stagehand_stubs, devtools_page, url: str) -> SandboxBrowser:
    browser = _browser()
    await browser.aensure()
    page = SimpleNamespace(page_id="T1", url=AsyncMock(return_value=url))
    browser.stagehand.browser.context = SimpleNamespace(active_page=AsyncMock(return_value=page))
    browser.tunnel.http_url = devtools_page.http_url
    return browser


async def test_a_failed_navigation_is_read_from_the_pages_history(stagehand_stubs, devtools_page):
    devtools_page.entries = ["http://localhost:5173/", "https://example.com/"]
    browser = await _browser_on_page(stagehand_stubs, devtools_page, "chrome-error://chromewebdata/")

    assert await browser.afailed_navigation() == "https://example.com/"
    assert devtools_page.paths == ["/devtools/page/T1"]


async def test_a_page_that_loaded_has_no_failed_navigation(stagehand_stubs, devtools_page):
    browser = await _browser_on_page(stagehand_stubs, devtools_page, "http://localhost:5173/")

    assert await browser.afailed_navigation() is None
    assert devtools_page.paths == []


async def test_an_unreadable_history_still_reports_a_failed_navigation(stagehand_stubs, devtools_page):
    devtools_page.entries = ["chrome-error://chromewebdata/"]
    browser = await _browser_on_page(stagehand_stubs, devtools_page, "chrome-error://chromewebdata/")

    assert await browser.afailed_navigation() == ""


async def test_no_failed_navigation_before_the_browser_starts(stagehand_stubs):
    assert await _browser().afailed_navigation() is None


async def test_two_calls_that_lose_the_browser_together_stop_it_once(stagehand_stubs):
    browser = _browser()
    await browser.aensure()

    async def drop(_facade):
        await asyncio.sleep(0)
        raise CDPConnectionClosedError

    results = await asyncio.gather(browser.acall(drop), browser.acall(drop), return_exceptions=True)

    assert all(isinstance(result, BrowserError) for result in results)
    assert browser.client.calls_to("stop_browser") == [("sess-1", False)]


async def test_a_late_failure_on_the_old_connection_leaves_the_restarted_browser_running(stagehand_stubs):
    browser = _browser()
    await browser.aensure()
    old_facade = browser.facade
    release_late_call = asyncio.Event()

    async def late(_facade):
        await release_late_call.wait()
        raise CDPConnectionClosedError

    async def drop(_facade):
        raise CDPConnectionClosedError

    late_call = asyncio.create_task(browser.acall(late))
    await asyncio.sleep(0)
    with pytest.raises(BrowserError):
        await browser.acall(drop)
    await browser.aensure()
    assert browser.facade is not old_facade
    release_late_call.set()

    with pytest.raises(BrowserError):
        await late_call
    assert browser.state == "ready"
    assert browser.client.calls_to("stop_browser") == [("sess-1", False)]
