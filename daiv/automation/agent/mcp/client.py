from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx2
from fastmcp import Client
from fastmcp.client.transports import SSETransport, StreamableHttpTransport
from langchain_core._api import suppress_langchain_beta_warning
from mcp.shared.exceptions import MCPError

with suppress_langchain_beta_warning():
    from langchain.mcp import as_langchain_tool

if TYPE_CHECKING:
    from fastmcp.client.logging import LogMessage
    from langchain_core.tools.base import BaseTool

_BENIGN_405_METHODS = frozenset({"GET", "DELETE"})
# mcp's own defaults; httpx2's 5s read timeout would cut off slow tool calls.
_DEFAULT_TIMEOUT = httpx2.Timeout(30.0, read=300.0)


class MCPHTTPStatusError(Exception):
    """An HTTP failure whose status mcp 2.x hid behind a generic ``MCPError``."""

    def __init__(self, status_code: int, reason: str, url: str):
        super().__init__(f"HTTP {status_code} {reason} for url '{url}'")
        self.status_code = status_code
        self.reason = reason
        self.url = url


@dataclass(frozen=True, slots=True)
class FailedResponse:
    status_code: int
    reason: str
    url: str


class StatusRecorder:
    """httpx2 response hook keeping the last failed (>= 400) response.

    A ``405`` on ``GET``/``DELETE`` is ignored: streamable HTTP lets a server refuse the standalone
    stream and session termination that way.
    """

    def __init__(self) -> None:
        self.last: FailedResponse | None = None

    async def __call__(self, response: httpx2.Response) -> None:
        if response.status_code < 400:
            return
        if response.status_code == 405 and response.request.method in _BENIGN_405_METHODS:
            return
        self.last = FailedResponse(response.status_code, response.reason_phrase, str(response.request.url))


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
    """List the server's tools as LangChain tools; HTTP failures raise ``MCPHTTPStatusError``."""
    try:
        async with client:
            return [await as_langchain_tool(tool, client) for tool in await client.list_tools()]
    except Exception as exc:
        failed = client.status_recorder.last
        if failed is not None and _is_http_shaped(exc):
            raise MCPHTTPStatusError(failed.status_code, failed.reason, failed.url) from exc
        raise


def _is_http_shaped(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(_is_http_shaped(sub) for sub in exc.exceptions)
    return isinstance(exc, MCPError | httpx2.HTTPStatusError)
