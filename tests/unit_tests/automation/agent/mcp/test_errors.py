import asyncio

import anyio
import httpx2
import pytest
from mcp.shared.exceptions import MCPError

from automation.agent.mcp.client import MCPHTTPStatusError
from automation.agent.mcp.errors import FailureKind, classify

URL = "https://mcp.example.com/mcp"


def _httpx_status_error(status: int) -> httpx2.HTTPStatusError:
    request = httpx2.Request("POST", URL)
    return httpx2.HTTPStatusError("boom", request=request, response=httpx2.Response(status, request=request))


def _connect_wrapper() -> RuntimeError:
    wrapper = RuntimeError("Client failed to connect: All connection attempts failed")
    wrapper.__cause__ = httpx2.ConnectError("All connection attempts failed")
    return wrapper


class _BlankError(Exception):
    def __str__(self) -> str:
        return ""


CASES = [
    pytest.param(
        MCPHTTPStatusError(401, "Unauthorized", URL),
        FailureKind.CLIENT_ERROR,
        f"HTTP 401 Unauthorized for url '{URL}'",
        id="status-error-401",
    ),
    pytest.param(
        MCPHTTPStatusError(503, "Service Unavailable", URL),
        FailureKind.SERVER_ERROR,
        f"HTTP 503 Service Unavailable for url '{URL}'",
        id="status-error-503",
    ),
    pytest.param(
        _httpx_status_error(503),
        FailureKind.SERVER_ERROR,
        f"HTTP 503 Service Unavailable for url '{URL}'",
        id="httpx2-status-503",
    ),
    pytest.param(
        _httpx_status_error(404),
        FailureKind.CLIENT_ERROR,
        f"HTTP 404 Not Found for url '{URL}'",
        id="httpx2-status-404",
    ),
    pytest.param(
        _connect_wrapper(),
        FailureKind.UNREACHABLE,
        "ConnectError: All connection attempts failed",
        id="connect-wrapper-followed",
    ),
    pytest.param(
        RuntimeError("Client failed to connect: nothing"),
        FailureKind.UNEXPECTED,
        "RuntimeError: Client failed to connect: nothing",
        id="connect-wrapper-without-cause",
    ),
    pytest.param(RuntimeError("boom"), FailureKind.UNEXPECTED, "RuntimeError: boom", id="other-runtime-error"),
    pytest.param(TimeoutError(), FailureKind.TIMEOUT, "TimeoutError", id="timeout"),
    pytest.param(OSError("no route"), FailureKind.UNREACHABLE, "OSError: no route", id="os-error"),
    pytest.param(httpx2.ReadTimeout("slow"), FailureKind.UNREACHABLE, "ReadTimeout: slow", id="httpx2-transport-error"),
    pytest.param(anyio.BrokenResourceError(), FailureKind.STREAM_BROKEN, "BrokenResourceError", id="broken-stream"),
    pytest.param(anyio.ClosedResourceError(), FailureKind.STREAM_BROKEN, "ClosedResourceError", id="closed-stream"),
    pytest.param(
        MCPError(-32603, "Server returned an error response"),
        FailureKind.PROTOCOL,
        "MCPError: Server returned an error response",
        id="bare-mcp-error",
    ),
    pytest.param(_BlankError(), FailureKind.UNEXPECTED, "_BlankError", id="blank-message-keeps-class-name"),
    pytest.param(
        ValueError("first line\nsecond line"), FailureKind.UNEXPECTED, "ValueError: first line", id="first-line-only"
    ),
    pytest.param(
        ExceptionGroup("g", [MCPHTTPStatusError(503, "Service Unavailable", URL)] * 2),
        FailureKind.SERVER_ERROR,
        f"HTTP 503 Service Unavailable for url '{URL}'",
        id="group-same-kind-deduplicated",
    ),
    pytest.param(
        ExceptionGroup("outer", [ExceptionGroup("inner", [anyio.BrokenResourceError()])]),
        FailureKind.STREAM_BROKEN,
        "BrokenResourceError",
        id="nested-groups-flattened",
    ),
    pytest.param(
        ExceptionGroup("g", [_httpx_status_error(503), ValueError("x")]),
        FailureKind.UNEXPECTED,
        f"HTTP 503 Service Unavailable for url '{URL}'; ValueError: x",
        id="mixed-group-is-unexpected",
    ),
    pytest.param(
        BaseExceptionGroup("g", [asyncio.CancelledError()]),
        FailureKind.UNEXPECTED,
        "CancelledError",
        id="cancellation-group-is-unexpected",
    ),
    pytest.param(
        ExceptionGroup("g", [ValueError("x"), ValueError("x")]),
        FailureKind.UNEXPECTED,
        "ValueError: x",
        id="duplicate-messages-collapsed",
    ),
]


@pytest.mark.parametrize(("exc", "kind", "message"), CASES)
def test_classify(exc, kind, message):
    failure = classify(exc)

    assert failure.kind is kind
    assert failure.message == message
