"""A worker-local port that forwards to a sandbox session browser's DevTools port.

The worker has no network path to the browser. Each TCP connection accepted on ``127.0.0.1`` is carried as binary
frames over one WebSocket to daiv-sandbox's ``/session/{id}/browser/cdp`` endpoint, which pipes it to the browser's
DevTools port. Stagehand then reaches the browser as it would a local Chrome; Chrome builds the WebSocket URLs it
advertises from the request's ``Host`` header, so they point back at this port.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Self

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

if TYPE_CHECKING:
    from websockets.asyncio.client import ClientConnection

    from .client import DAIVSandboxClient

logger = logging.getLogger("daiv.sandbox")

_READ_CHUNK = 64 * 1024


class BrowserTunnel:
    """One run's forwarded DevTools port; ``aclose`` closes the listener and every connection it bridged."""

    def __init__(self, client: DAIVSandboxClient, session_id: str) -> None:
        self._url = client.browser_cdp_url(session_id)
        self._headers = client.auth_headers()
        self._server: asyncio.Server | None = None
        self._bridges: set[asyncio.Task[None]] = set()

    async def aopen(self) -> Self:
        self._server = await asyncio.start_server(self._bridge, "127.0.0.1", 0)
        return self

    @property
    def http_url(self) -> str:
        if self._server is None:
            raise RuntimeError("The browser tunnel is not open")
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def aclose(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
        bridges = list(self._bridges)
        for bridge in bridges:
            bridge.cancel()
        await asyncio.gather(*bridges, return_exceptions=True)
        if server is not None:
            await server.wait_closed()

    async def _bridge(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._bridges.add(task)
        try:
            async with connect(
                self._url, additional_headers=self._headers, max_size=None, proxy=None, compression=None
            ) as websocket:
                await self._pump(reader, writer, websocket)
        except (OSError, WebSocketException) as exc:
            logger.warning("Browser tunnel connection ended: %s", type(exc).__name__)
        finally:
            if task is not None:
                self._bridges.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, websocket: ClientConnection) -> None:
        async def upstream() -> None:
            while data := await reader.read(_READ_CHUNK):
                await websocket.send(data)

        async def downstream() -> None:
            async for message in websocket:
                if isinstance(message, str):
                    return
                writer.write(message)
                await writer.drain()

        tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for task in done:
            if not task.cancelled() and (exc := task.exception()) is not None:
                raise exc
