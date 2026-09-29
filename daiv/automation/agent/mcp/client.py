from __future__ import annotations

from typing import TYPE_CHECKING

import httpx2
from fastmcp import Client
from fastmcp.client.transports import SSETransport, StreamableHttpTransport
from langchain_core._api import suppress_langchain_beta_warning
from mcp.shared.exceptions import MCPError

from .errors import status_message

with suppress_langchain_beta_warning():
    from langchain.mcp import as_langchain_tool

if TYPE_CHECKING:
    from fastmcp.client.logging import LogMessage
    from langchain_core.tools.base import BaseTool

# mcp's own defaults; httpx2's 5s read timeout would cut off slow tool calls.
_DEFAULT_TIMEOUT = httpx2.Timeout(30.0, read=300.0)


class StatusRecorder:
    """httpx2 response hook keeping the latest ``POST`` response if it failed, cleared when one succeeds.

    Only a ``POST`` failure reaches the caller as an ``MCPError``: mcp recovers from a rejected
    ``server/discover`` probe and swallows failures of the standalone ``GET`` stream and the teardown ``DELETE``.
    """

    def __init__(self) -> None:
        self.last: httpx2.Response | None = None

    async def __call__(self, response: httpx2.Response) -> None:
        if response.request.method == "POST":
            self.last = response if response.is_error else None


async def _drop_server_log(message: LogMessage) -> None:
    """fastmcp's default handler would re-emit server-chosen log levels on its own unfiltered logger."""


class StatusTrackingClient(Client):
    def __init__(self, transport, status_recorder: StatusRecorder):
        super().__init__(transport, log_handler=_drop_server_log)
        self.status_recorder = status_recorder


def build_client(
    transport: str, url: str, headers: dict[str, str] | None, *, http_transport: httpx2.AsyncBaseTransport | None = None
) -> StatusTrackingClient:
    """Build a plain client (no elicitation/sampling handlers) whose HTTP failures are recorded.

    ``http_transport`` is the test seam (e.g. ``httpx2.ASGITransport``); ``None`` in production.
    """
    recorder = StatusRecorder()

    def httpx_client_factory(
        headers: dict[str, str] | None = None,
        timeout: httpx2.Timeout | None = None,
        auth: httpx2.Auth | None = None,
        **kwargs,
    ) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            headers=headers,
            timeout=timeout or _DEFAULT_TIMEOUT,
            auth=auth,
            transport=http_transport,
            event_hooks={"response": [recorder]},
            **kwargs,
        )

    if transport == "http":
        mcp_transport = StreamableHttpTransport(url, headers=headers, httpx_client_factory=httpx_client_factory)
    elif transport == "sse":
        mcp_transport = SSETransport(url, headers=headers, httpx_client_factory=httpx_client_factory)
    else:
        raise ValueError(f"Unsupported transport: {transport!r}")
    return StatusTrackingClient(mcp_transport, recorder)


async def list_tools(client: StatusTrackingClient) -> list[BaseTool]:
    """List the server's tools as LangChain tools; HTTP failures raise ``httpx2.HTTPStatusError``.

    mcp 2.x hides a failed POST's status behind a generic ``MCPError``, so it is restored from the recorder.
    """
    try:
        async with client:
            if client.initialize_result is not None:
                # A handshake-era server: skip the rejected ``server/discover`` probe on every tool-call reconnect.
                client.mode = "legacy"
            return [await as_langchain_tool(tool, client) for tool in await client.list_tools()]
    except Exception as exc:
        failed = client.status_recorder.last
        if failed is not None and _is_http_shaped(exc):
            raise httpx2.HTTPStatusError(status_message(failed), request=failed.request, response=failed) from exc
        raise


def _is_http_shaped(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(_is_http_shaped(sub) for sub in exc.exceptions)
    return isinstance(exc, MCPError | httpx2.HTTPStatusError)
