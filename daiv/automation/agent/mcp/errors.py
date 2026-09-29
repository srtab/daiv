from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import anyio
import httpx2
from mcp.shared.exceptions import MCPError
from mcp_types import CONNECTION_CLOSED

_CONNECT_WRAPPER_PREFIX = "Client failed to connect"
# How mcp's own client reports a dropped stream; servers may reuse the code for their own errors.
_STREAM_CLOSED_PREFIXES = ("Connection closed", "SSE stream ended")


class FailureKind(StrEnum):
    TIMEOUT = "timeout"
    SERVER_ERROR = "server_error"
    CLIENT_ERROR = "client_error"
    UNREACHABLE = "unreachable"
    STREAM_BROKEN = "stream_broken"
    PROTOCOL = "protocol"
    UNEXPECTED = "unexpected"


@dataclass(frozen=True, slots=True)
class MCPFailure:
    kinds: frozenset[FailureKind]
    message: str


def classify(exc: BaseException) -> MCPFailure:
    """Classify an MCP load/probe failure into the kinds of its leaf exceptions and a one-line message."""
    leaves = _leaves(exc)
    return MCPFailure(
        frozenset(_kind(leaf) for leaf in leaves), "; ".join(dict.fromkeys(_message(leaf) for leaf in leaves))
    )


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _leaves(sub)]
    if type(exc) is RuntimeError and str(exc).startswith(_CONNECT_WRAPPER_PREFIX) and exc.__cause__ is not None:
        return _leaves(exc.__cause__)
    return [exc]


def status_message(response: httpx2.Response) -> str:
    return f"HTTP {response.status_code} {response.reason_phrase} for url '{response.request.url}'"


def _kind(leaf: BaseException) -> FailureKind:
    if isinstance(leaf, httpx2.HTTPStatusError):
        return FailureKind.SERVER_ERROR if leaf.response.is_server_error else FailureKind.CLIENT_ERROR
    if isinstance(leaf, TimeoutError):
        return FailureKind.TIMEOUT
    if isinstance(leaf, anyio.BrokenResourceError | anyio.ClosedResourceError):
        return FailureKind.STREAM_BROKEN
    if isinstance(leaf, OSError | httpx2.TransportError | httpx2.InvalidURL):
        return FailureKind.UNREACHABLE
    if isinstance(leaf, MCPError):
        if leaf.code == CONNECTION_CLOSED and leaf.message.startswith(_STREAM_CLOSED_PREFIXES):
            return FailureKind.STREAM_BROKEN
        return FailureKind.PROTOCOL
    return FailureKind.UNEXPECTED


def _message(leaf: BaseException) -> str:
    if isinstance(leaf, httpx2.HTTPStatusError):
        return status_message(leaf.response)
    first_line = next((line for line in str(leaf).splitlines() if line.strip()), "")
    name = type(leaf).__name__
    return f"{name}: {first_line}" if first_line else name
