"""The headless browser a sandbox run drives, beside its sandbox session.

daiv-sandbox runs the browser in a container of its own and keeps the session's profile there between runs. This
module starts it on the agent's first browser call, reaches it through a ``BrowserTunnel``, restarts it from the saved
profile after a crash, and stops it when the run ends.

A start failure or a lost connection reaches the agent as one of the fixed texts below, never as an exception, the
service's address or its key; the cause goes to the logs. Stagehand's own tool errors pass through, as the agent acts
on them.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit, urlunsplit

from django.core.cache import cache

import httpx
from stagehand import Stagehand, get_extension_archive, local_browser
from stagehand.cdp_client import CDPConnectionClosedError
from stagehand.rpc_client import RPCError
from stagehand_middleware.facade import StagehandFacadeTools
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from core.sandbox.schemas import BrowserStartRequest
from core.sandbox.tunnel import BrowserTunnel

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from automation.agent.workspace.session import SandboxSession
    from core.sandbox.client import DAIVSandboxClient
    from core.sandbox.schemas import BrowserStartResponse, BrowserStopResponse

logger = logging.getLogger("daiv.tools")

BrowserState = Literal["idle", "starting", "ready", "lost", "restarting", "unavailable", "released"]

START_BUDGET_SECONDS = 30
RELEASE_BUDGET_SECONDS = 60
DISCONNECT_BUDGET_SECONDS = 10
EXTENSION_CHECK_BUDGET_SECONDS = 5
MAX_START_FAILURES = 2
NAVIGATION_HISTORY_BUDGET_SECONDS = 5
EXTENSION_CACHE_TTL = 24 * 60 * 60
CHROME_ERROR_URL_PREFIX = "chrome-error://"
# A path under a file, so every write fails, as root too: nothing on the worker reads run-code screenshots.
REFUSED_ARTIFACT_ROOT = "/dev/null/daiv-browser-artifacts"

START_FAILED_TEXT = "error: The browser could not start. Try again; if it fails again, continue without it."
UNAVAILABLE_TEXT = "error: The browser is unavailable for this run."
OOM_REASON = "it ran out of memory"
CONNECTION_LOST_REASON = "the connection was lost"


def lost_text(reason: str) -> str:
    return (
        f"error: The browser stopped ({reason}). "
        "The next browser call restarts it from this session's last saved profile."
    )


def restarted_note(reason: str) -> str:
    return (
        f"The browser stopped ({reason}) and restarted from this session's last saved profile; "
        "pages and state since then were lost."
    )


def extension_cache_key(session_id: str) -> str:
    """Where the digest of the extension a session's browser container holds is kept."""
    return f"sandbox:browser-extension:{session_id}"


def profile_note(response: BrowserStartResponse) -> str | None:
    """What the first result of a run says about the profile the browser started from; nothing for an empty one."""
    if response.profile == "reset":
        return "This session's browser profile was reset; the browser started empty."
    if response.profile == "restored":
        if response.restored_urls:
            urls = ", ".join(_shown_url(url) for url in response.restored_urls)
            return f"Browser restored from this session's profile; re-opened: {urls}."
        return "Browser restored from this session's profile (cookies and storage kept, no open pages)."
    return None


def _shown_url(url: str) -> str:
    """A restored tab's address without credentials, query or fragment, which may hold tokens; only the scheme of an
    address that is not a web page, whose text could be anything."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return f"{parts.scheme}:"
    return urlunsplit((parts.scheme, parts.netloc.rpartition("@")[2], parts.path, "", ""))


async def _attempted_url(http_url: str, target_id: str) -> str:
    """The current navigation entry's URL, which on Chromium's error page is the URL that failed to load."""
    async with connect(
        http_url.replace("http://", "ws://", 1) + f"/devtools/page/{target_id}", max_size=None, proxy=None
    ) as websocket:
        await websocket.send(json.dumps({"id": 1, "method": "Page.getNavigationHistory"}))
        async for message in websocket:
            reply = json.loads(message)
            if reply.get("id") != 1:
                continue
            history = reply.get("result") or {}
            entries = history.get("entries") or []
            index = history.get("currentIndex", len(entries) - 1)
            url = entries[index].get("url", "") if 0 <= index < len(entries) else ""
            return "" if url.startswith(CHROME_ERROR_URL_PREFIX) else url
    return ""


class BrowserError(Exception):
    """A browser failure the agent is told about with ``str(error)``, a fixed text."""


@functools.cache
def _extension() -> tuple[bytes, str]:
    archive = get_extension_archive()
    return archive, hashlib.sha256(archive).hexdigest()


class SandboxBrowser:
    """One run's browser: idle until the first tool call, released when the run ends.

    A lost connection stops the browser container, so the next call restarts it from the profile that stop saved. Two
    failed starts in a run make it unavailable for the rest of the run.

    Args:
        session: The run's sandbox session; its id is read when the browser starts.
        client: The run's sandbox transport.
        memory_bytes: The browser's memory limit, snapshotted when the run starts.
        cpus: The browser's CPU limit, snapshotted when the run starts.
    """

    def __init__(self, session: SandboxSession, client: DAIVSandboxClient, *, memory_bytes: int, cpus: float) -> None:
        self.session = session
        self.client = client
        self.limits = (memory_bytes, cpus)
        self.state: BrowserState = "idle"
        self.tunnel: BrowserTunnel | None = None
        self.stagehand: Stagehand | None = None
        self.facade: StagehandFacadeTools | None = None
        self.pending_note: str | None = None
        self.start_failures = 0
        self._session_id: str | None = None
        self._lost_reason = CONNECTION_LOST_REASON
        self._lock = asyncio.Lock()

    def take_note(self) -> str | None:
        note, self.pending_note = self.pending_note, None
        return note

    async def acall[T](self, operation: Callable[[StagehandFacadeTools], Awaitable[T]]) -> T:
        """Run ``operation`` on the facade, starting the browser first if needed.

        A facade error is raised as it is; a lost connection raises a ``BrowserError`` after the browser is stopped. An
        RPC error or a timeout counts as a lost connection when Stagehand's extension can no longer reach Chromium: its
        own connection dies with Chromium's network service, while the worker's stays up.
        """
        facade = await self.aensure()
        stagehand = self.stagehand
        try:
            return await operation(facade)
        except Exception as error:
            if not (self._connection_lost(error, stagehand) or await self._extension_lost(error, stagehand)):
                raise
            raise BrowserError(await self._on_lost(stagehand)) from None

    async def aensure(self) -> StagehandFacadeTools:
        async with self._lock:
            if self.state == "ready" and self.facade is not None:
                return self.facade
            if self.state in ("unavailable", "released"):
                raise BrowserError(UNAVAILABLE_TEXT)
            restarting = self.state == "lost"
            self.state = "restarting" if restarting else "starting"
            try:
                async with asyncio.timeout(START_BUDGET_SECONDS):
                    response, facade = await self._start()
            except Exception:
                await self._discard_connection()
                self.start_failures += 1
                logger.exception(
                    "Browser %s failed for sandbox session %s (%d of %d)",
                    "restart" if restarting else "start",
                    self.session.session_id,
                    self.start_failures,
                    MAX_START_FAILURES,
                )
                if self.start_failures >= MAX_START_FAILURES:
                    self.state = "unavailable"
                    raise BrowserError(UNAVAILABLE_TEXT) from None
                self.state = "lost" if restarting else "idle"
                raise BrowserError(START_FAILED_TEXT) from None
            self.state = "ready"
            if not restarting:
                self.pending_note = profile_note(response)
            return facade

    async def afailed_navigation(self) -> str | None:
        """The URL the active page failed to load, ``""`` when it cannot be told, or ``None`` when no load failed.

        A refused navigation does not raise: Chromium shows its error page, whose own URL hides the one attempted, so
        that one is read from the page's navigation history.
        """
        stagehand, tunnel = self.stagehand, self.tunnel
        if stagehand is None or tunnel is None:
            return None
        try:
            page = await stagehand.browser.context.active_page()
            if page is None or not (await page.url()).startswith(CHROME_ERROR_URL_PREFIX):
                return None
        except Exception:
            logger.warning("Could not read the active page's URL", exc_info=True)
            return None
        try:
            async with asyncio.timeout(NAVIGATION_HISTORY_BUDGET_SECONDS):
                return await _attempted_url(tunnel.http_url, page.page_id)
        except Exception:
            logger.warning("Could not read the navigation history of page %s", page.page_id, exc_info=True)
            return ""

    async def arelease(self, *, stop: bool = True) -> None:
        """Let go of the browser when the run ends; ``stop`` also stops its container, which saves the profile.

        Never raises: a failure is logged, and the sandbox's own stop or the reaper reclaims the container.
        """
        if self.state == "released":
            return
        self.state = "released"
        try:
            async with asyncio.timeout(RELEASE_BUDGET_SECONDS):
                await self._discard_connection()
                if stop and self._session_id is not None:
                    await self.client.stop_browser(self._session_id)
        except Exception:
            logger.exception("Could not release the browser of sandbox session %s", self.session.session_id)

    async def _start(self) -> tuple[BrowserStartResponse, StagehandFacadeTools]:
        session_id = self.session.session_id
        if session_id is None:
            raise RuntimeError("The sandbox session is not acquired")
        self._session_id = session_id
        archive, digest = _extension()
        memory_bytes, cpus = self.limits
        response = await self._start_container(
            session_id, BrowserStartRequest(memory_bytes=memory_bytes, cpus=cpus, extension_sha256=digest), archive
        )
        self.tunnel = await BrowserTunnel(self.client, session_id).aopen()
        browser = await local_browser.connect(
            cdp_url=self.tunnel.http_url,
            extension_cdp_url=f"ws://127.0.0.1:{response.devtools_port}{response.browser_ws_path}",
        )
        self.stagehand = stagehand = await Stagehand.create(browser=browser, logging={"level": "off"})
        self.facade = facade = self._new_facade(stagehand)
        return response, facade

    async def _start_container(
        self, session_id: str, request: BrowserStartRequest, archive: bytes
    ) -> BrowserStartResponse:
        """Leaves the extension archive out when the session's container already holds it, and sends it on the 412 a
        container that lost it answers."""
        key = extension_cache_key(session_id)
        if await cache.aget(key) == request.extension_sha256:
            try:
                return await self.client.start_browser(session_id, request, None)
            except httpx.HTTPStatusError as error:
                if error.response.status_code != httpx.codes.PRECONDITION_FAILED:
                    raise
        response = await self.client.start_browser(session_id, request, archive)
        await cache.aset(key, request.extension_sha256, EXTENSION_CACHE_TTL)
        return response

    def _new_facade(self, stagehand: Stagehand) -> StagehandFacadeTools:
        return StagehandFacadeTools(
            stagehand, artifact_root=REFUSED_ARTIFACT_ROOT, on_close_requested=self._reopen_facade
        )

    async def _reopen_facade(self) -> None:
        """Run code that calls ``browser.close()`` gets a fresh facade over the same browser, which the run owns."""
        if self.stagehand is not None:
            self.facade = self._new_facade(self.stagehand)

    @staticmethod
    def _connection_lost(error: BaseException, stagehand: Stagehand | None) -> bool:
        if isinstance(error, (CDPConnectionClosedError, ConnectionClosed, ConnectionError)):
            return True
        return stagehand is not None and not stagehand.browser.connected

    @staticmethod
    async def _extension_lost(error: BaseException, stagehand: Stagehand | None) -> bool:
        """Whether the extension fails a cookie read, which needs its own connection to Chromium, after ``error``."""
        if stagehand is None or not isinstance(error, (RPCError, TimeoutError)):
            return False
        try:
            async with asyncio.timeout(EXTENSION_CHECK_BUDGET_SECONDS):
                await stagehand.browser.context.cookies(urls="about:blank")
        except Exception as check_error:
            logger.warning("Browser extension cannot reach Chromium after a %s: %r", type(error).__name__, check_error)
            return True
        return False

    async def _on_lost(self, stagehand: Stagehand | None) -> str:
        """Stop the browser whose connection ``stagehand`` held, unless it was already stopped or replaced, and say
        what the agent is told."""
        async with self._lock:
            if self.state == "lost" or self.stagehand is not stagehand:
                return self._stale_connection_text()
            self.state = "lost"
            await self._discard_connection()
            stopped = await self._stop_after_loss()
            oom = stopped is not None and stopped.oom_killed
            reason = self._lost_reason = OOM_REASON if oom else CONNECTION_LOST_REASON
            logger.log(
                logging.ERROR if oom else logging.WARNING,
                "Browser of sandbox session %s stopped: %s",
                self.session.session_id,
                reason,
            )
            self.pending_note = restarted_note(reason)
            return lost_text(reason)

    def _stale_connection_text(self) -> str:
        """What a call on a connection already stopped or replaced is told, from the state it finds."""
        if self.state == "ready":
            return f"error: {self.take_note() or restarted_note(self._lost_reason)}"
        if self.state in ("unavailable", "released"):
            return UNAVAILABLE_TEXT
        return lost_text(self._lost_reason)

    async def _stop_after_loss(self) -> BrowserStopResponse | None:
        if self._session_id is None:
            return None
        try:
            async with asyncio.timeout(RELEASE_BUDGET_SECONDS):
                return await self.client.stop_browser(self._session_id)
        except Exception:
            logger.exception("Could not stop the lost browser of sandbox session %s", self.session.session_id)
            return None

    async def _discard_connection(self) -> None:
        stagehand, tunnel = self.stagehand, self.tunnel
        self.stagehand = self.tunnel = self.facade = None
        try:
            if stagehand is not None:
                async with asyncio.timeout(DISCONNECT_BUDGET_SECONDS):
                    await stagehand.browser.disconnect()
        except Exception:
            logger.warning("Could not disconnect Stagehand from the browser cleanly", exc_info=True)
        finally:
            if tunnel is not None:
                await tunnel.aclose()
