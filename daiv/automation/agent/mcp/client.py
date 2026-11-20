from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import httpx2
from fastmcp import Client
from fastmcp.client.transports import SSETransport, StreamableHttpTransport
from langchain_core._api import suppress_langchain_beta_warning
from langchain_core.tools import StructuredTool, ToolException
from langgraph.errors import GraphBubbleUp
from mcp.shared.exceptions import MCPError

from .errors import SOFT_FAILURES, classify, status_message

with suppress_langchain_beta_warning():
    from langchain.mcp import as_langchain_tool

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from fastmcp.client.logging import LogMessage
    from langchain_core.tools.base import BaseTool
    from mcp_types import Icon

logger = logging.getLogger("daiv.tools")

# mcp's own defaults; httpx2's 5s read timeout would cut off slow tool calls.
_DEFAULT_TIMEOUT = httpx2.Timeout(30.0, read=300.0)
# Fits a base64 screenshot (mcp's 1 MiB default does not) within Anthropic's 5 MiB image limit.
# The legacy SSE transport keeps mcp's hardcoded 1 MiB.
_MAX_STREAMABLE_HTTP_EVENT_SIZE = 5 * 1024 * 1024

_call_status: ContextVar[StatusRecorder | None] = ContextVar("mcp_call_status", default=None)


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


async def _record_call_status(response: httpx2.Response) -> None:
    """Record ``response`` for the tool call that sent it.

    Streamable HTTP sends each request in its sender's ``contextvars`` context, so concurrent calls don't mix.
    """
    if (recorder := _call_status.get()) is not None:
        await recorder(response)


@contextmanager
def _recording_status(recorder: StatusRecorder | None) -> Generator[None]:
    token = _call_status.set(recorder)
    try:
        yield
    finally:
        _call_status.reset(token)


async def _drop_server_log(message: LogMessage) -> None:
    """fastmcp's default handler would re-emit server-chosen log levels on its own unfiltered logger."""


class StatusTrackingClient(Client):
    def __init__(self, transport):
        super().__init__(transport, log_handler=_drop_server_log)


def build_client(
    transport: str, url: str, headers: dict[str, str] | None, *, http_transport: httpx2.AsyncBaseTransport | None = None
) -> StatusTrackingClient:
    """Build a plain client (no elicitation/sampling handlers) whose HTTP failures are recorded.

    ``http_transport`` is the test seam (e.g. ``httpx2.ASGITransport``); ``None`` in production.
    """

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
            event_hooks={"response": [_record_call_status]},
            **kwargs,
        )

    if transport == "http":
        mcp_transport = StreamableHttpTransport(
            url,
            headers=headers,
            httpx_client_factory=httpx_client_factory,
            max_sse_event_size=_MAX_STREAMABLE_HTTP_EVENT_SIZE,
        )
    elif transport == "sse":
        mcp_transport = SSETransport(url, headers=headers, httpx_client_factory=httpx_client_factory)
    else:
        raise ValueError(f"Unsupported transport: {transport!r}")
    return StatusTrackingClient(mcp_transport)


_ICON_DATA_URI = re.compile(r"data:image/(png|jpeg|webp|gif|svg\+xml)[;,]")
_MAX_ICON_SRC_LENGTH = 32768


def pick_icon_src(icons: Sequence[Icon] | None) -> str | None:
    """The first icon a page can safely show as an ``<img>``: an ``https:`` URL or a small image ``data:`` URI."""
    for icon in icons or []:
        src = icon.src
        if len(src) > _MAX_ICON_SRC_LENGTH:
            continue
        if src.startswith("https:") or _ICON_DATA_URI.match(src):
            return src
    return None


async def list_tools(client: StatusTrackingClient) -> list[BaseTool]:
    """List the server's tools as LangChain tools; HTTP failures raise ``httpx2.HTTPStatusError``."""
    tools, _ = await list_tools_and_icon(client)
    return tools


async def list_tools_and_icon(client: StatusTrackingClient) -> tuple[list[BaseTool], str | None]:
    """:func:`list_tools` plus the server's own icon (``None`` when it advertises none).

    mcp 2.x hides a failed POST's status behind a generic ``MCPError``, so it is restored from the recorder.
    """
    recorder = StatusRecorder()
    try:
        with _recording_status(recorder):
            async with client:
                if client.initialize_result is not None:
                    # A handshake-era server: skip the rejected ``server/discover`` probe on every tool-call reconnect.
                    client.mode = "legacy"
                tools = [await as_langchain_tool(tool, client) for tool in await client.list_tools()]
                server_info = client.server_info
                icon = pick_icon_src(server_info.icons if server_info is not None else None)
    except Exception as exc:
        if (restored := _restore_status(recorder, exc)) is not None:
            raise restored from exc
        raise
    for tool in tools:
        _report_call_failures(tool, client)
    return tools, icon


def read_only_hint(tool: BaseTool) -> bool | None:
    """MCP's optional ``readOnlyHint`` annotation; ``None`` means the server left it unannotated."""
    node: Any = tool.metadata
    for key in ("mcp", "tool", "annotations"):
        node = node.get(key) if isinstance(node, dict) else None
    return node.get("read_only_hint") if isinstance(node, dict) else None


class MCPCallError(ToolException):
    """A tool call that failed before the server answered, e.g. a dropped stream or an HTTP error."""


def _report_call_failures(tool: BaseTool, client: StatusTrackingClient) -> None:
    """Turn a failed call of ``tool`` into an error ``ToolMessage`` instead of an exception that ends the run.

    ``langchain.mcp`` handles only the results a server marks ``isError``; every other failure escapes the ToolNode.
    """
    if not isinstance(tool, StructuredTool) or tool.coroutine is None or not callable(tool.handle_tool_error):
        raise TypeError(f"langchain.mcp built tool {tool.name!r} in an unexpected shape")
    call, handle_server_error = tool.coroutine, tool.handle_tool_error
    tracks_status = isinstance(client.transport, StreamableHttpTransport)
    retry_note = (
        "" if read_only_hint(tool) else "\nThe server may have run it anyway: check its effect before retrying."
    )

    async def coroutine(**arguments):
        recorder = StatusRecorder() if tracks_status else None
        try:
            with _recording_status(recorder):
                return await call(**arguments)
        except ToolException, GraphBubbleUp:
            raise
        except Exception as exc:  # never BaseException: CancelledError must propagate
            failure = classify(_restore_status(recorder, exc) or exc)
            logger.error(
                "MCP tool %r failed: %s", tool.name, failure.message, exc_info=not failure.kinds <= SOFT_FAILURES
            )
            raise MCPCallError(f"The MCP tool call failed: {failure.message}{retry_note}") from exc

    def handle_tool_error(error: ToolException):
        return str(error) if isinstance(error, MCPCallError) else handle_server_error(error)

    tool.coroutine = coroutine
    tool.handle_tool_error = handle_tool_error


def _restore_status(recorder: StatusRecorder | None, exc: Exception) -> httpx2.HTTPStatusError | None:
    failed = recorder.last if recorder is not None else None
    if failed is not None and _is_http_shaped(exc):
        return httpx2.HTTPStatusError(status_message(failed), request=failed.request, response=failed)
    return None


def _is_http_shaped(exc: BaseException) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(_is_http_shaped(sub) for sub in exc.exceptions)
    return isinstance(exc, MCPError | httpx2.HTTPStatusError)
