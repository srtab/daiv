import asyncio

import anyio
import httpx2
import pytest
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED

from automation.agent.mcp.errors import FailureKind, classify
from tests.unit_tests.automation.agent.mcp.helpers import status_error

URL = "https://mcp.example.com/mcp"


def _connect_wrapper() -> RuntimeError:
    wrapper = RuntimeError("Client failed to connect: All connection attempts failed")
    wrapper.__cause__ = httpx2.ConnectError("All connection attempts failed")
    return wrapper


class _BlankError(Exception):
    def __str__(self) -> str:
        return ""


CASES = [
    pytest.param(
        status_error(401, URL), {FailureKind.CLIENT_ERROR}, f"HTTP 401 Unauthorized for url '{URL}'", id="http-401"
    ),
    pytest.param(
        status_error(503, URL),
        {FailureKind.SERVER_ERROR},
        f"HTTP 503 Service Unavailable for url '{URL}'",
        id="http-503",
    ),
    pytest.param(
        httpx2.HTTPStatusError(
            "Server error '502 Bad Gateway' for url\nFor more information check: https://example.com",
            request=httpx2.Request("GET", URL),
            response=httpx2.Response(502, request=httpx2.Request("GET", URL)),
        ),
        {FailureKind.SERVER_ERROR},
        f"HTTP 502 Bad Gateway for url '{URL}'",
        id="native-httpx2-message-is-rebuilt",
    ),
    pytest.param(
        _connect_wrapper(),
        {FailureKind.UNREACHABLE},
        "ConnectError: All connection attempts failed",
        id="connect-wrapper-followed",
    ),
    pytest.param(
        RuntimeError("Client failed to connect: nothing"),
        {FailureKind.UNEXPECTED},
        "RuntimeError: Client failed to connect: nothing",
        id="connect-wrapper-without-cause",
    ),
    pytest.param(RuntimeError("boom"), {FailureKind.UNEXPECTED}, "RuntimeError: boom", id="other-runtime-error"),
    pytest.param(TimeoutError(), {FailureKind.TIMEOUT}, "TimeoutError", id="timeout"),
    pytest.param(OSError("no route"), {FailureKind.UNREACHABLE}, "OSError: no route", id="os-error"),
    pytest.param(
        httpx2.ReadTimeout("slow"), {FailureKind.UNREACHABLE}, "ReadTimeout: slow", id="httpx2-transport-error"
    ),
    pytest.param(anyio.BrokenResourceError(), {FailureKind.STREAM_BROKEN}, "BrokenResourceError", id="broken-stream"),
    pytest.param(anyio.ClosedResourceError(), {FailureKind.STREAM_BROKEN}, "ClosedResourceError", id="closed-stream"),
    pytest.param(
        MCPError(-32603, "Server returned an error response"),
        {FailureKind.PROTOCOL},
        "MCPError: Server returned an error response",
        id="bare-mcp-error",
    ),
    pytest.param(
        MCPError(CONNECTION_CLOSED, "Connection closed"),
        {FailureKind.STREAM_BROKEN},
        "MCPError: Connection closed",
        id="connection-closed",
    ),
    pytest.param(
        MCPError(CONNECTION_CLOSED, "SSE stream ended without a response"),
        {FailureKind.STREAM_BROKEN},
        "MCPError: SSE stream ended without a response",
        id="sse-stream-ended",
    ),
    pytest.param(
        MCPError(CONNECTION_CLOSED, "Bad Request: No valid session ID provided"),
        {FailureKind.PROTOCOL},
        "MCPError: Bad Request: No valid session ID provided",
        id="server-error-sharing-the-connection-closed-code",
    ),
    pytest.param(_BlankError(), {FailureKind.UNEXPECTED}, "_BlankError", id="blank-message-keeps-class-name"),
    pytest.param(
        ValueError("first line\nsecond line"), {FailureKind.UNEXPECTED}, "ValueError: first line", id="first-line-only"
    ),
    pytest.param(
        ExceptionGroup("g", [status_error(503, URL)] * 2),
        {FailureKind.SERVER_ERROR},
        f"HTTP 503 Service Unavailable for url '{URL}'",
        id="group-same-kind-deduplicated",
    ),
    pytest.param(
        ExceptionGroup("outer", [ExceptionGroup("inner", [anyio.BrokenResourceError()])]),
        {FailureKind.STREAM_BROKEN},
        "BrokenResourceError",
        id="nested-groups-flattened",
    ),
    pytest.param(
        ExceptionGroup("g", [status_error(503, URL), anyio.BrokenResourceError()]),
        {FailureKind.SERVER_ERROR, FailureKind.STREAM_BROKEN},
        f"HTTP 503 Service Unavailable for url '{URL}'; BrokenResourceError",
        id="mixed-group-keeps-every-kind",
    ),
    pytest.param(
        ExceptionGroup("g", [status_error(503, URL), ValueError("x")]),
        {FailureKind.SERVER_ERROR, FailureKind.UNEXPECTED},
        f"HTTP 503 Service Unavailable for url '{URL}'; ValueError: x",
        id="mixed-group-with-a-bug",
    ),
    pytest.param(
        BaseExceptionGroup("g", [asyncio.CancelledError()]),
        {FailureKind.UNEXPECTED},
        "CancelledError",
        id="cancellation-group-is-unexpected",
    ),
    pytest.param(
        ExceptionGroup("g", [ValueError("x"), ValueError("x")]),
        {FailureKind.UNEXPECTED},
        "ValueError: x",
        id="duplicate-messages-collapsed",
    ),
]


@pytest.mark.parametrize(("exc", "kinds", "message"), CASES)
def test_classify(exc, kinds, message):
    failure = classify(exc)

    assert failure.kinds == kinds
    assert failure.message == message
