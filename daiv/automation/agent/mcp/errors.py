from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import anyio
import httpx2
from mcp.shared.exceptions import MCPError

from .client import MCPHTTPStatusError

_CONNECT_WRAPPER_PREFIX = "Client failed to connect"


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
    kind: FailureKind
    message: str


def classify(exc: BaseException) -> MCPFailure:
    """Classify an MCP load/probe failure and render its user-facing message.

    Shared by the runtime toolkit (log level) and test-connection (log level + UI message).
    """
    leaves = _leaves(exc)
    kinds = {_kind(leaf) for leaf in leaves}
    kind = kinds.pop() if len(kinds) == 1 else FailureKind.UNEXPECTED
    return MCPFailure(kind, "; ".join(dict.fromkeys(_message(leaf) for leaf in leaves)))


def _leaves(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _leaves(sub)]
    if type(exc) is RuntimeError and str(exc).startswith(_CONNECT_WRAPPER_PREFIX) and exc.__cause__ is not None:
        return _leaves(exc.__cause__)
    return [exc]


def _http_status(leaf: BaseException) -> tuple[int, str, str] | None:
    if isinstance(leaf, MCPHTTPStatusError):
        return leaf.status_code, leaf.reason, leaf.url
    if isinstance(leaf, httpx2.HTTPStatusError):
        return leaf.response.status_code, leaf.response.reason_phrase, str(leaf.request.url)
    return None


def _kind(leaf: BaseException) -> FailureKind:
    status = _http_status(leaf)
    if status is not None:
        return FailureKind.SERVER_ERROR if status[0] >= 500 else FailureKind.CLIENT_ERROR
    if isinstance(leaf, TimeoutError):
        return FailureKind.TIMEOUT
    if isinstance(leaf, anyio.BrokenResourceError | anyio.ClosedResourceError):
        return FailureKind.STREAM_BROKEN
    if isinstance(leaf, OSError | httpx2.TransportError | httpx2.InvalidURL):
        return FailureKind.UNREACHABLE
    if isinstance(leaf, MCPError):
        return FailureKind.PROTOCOL
    return FailureKind.UNEXPECTED


def _message(leaf: BaseException) -> str:
    status = _http_status(leaf)
    if status is not None:
        code, reason, url = status
        return f"HTTP {code} {reason} for url '{url}'"
    first_line = next((line for line in str(leaf).splitlines() if line.strip()), "")
    name = type(leaf).__name__
    return f"{name}: {first_line}" if first_line else name
