from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx
from langchain_mcp_adapters.client import MultiServerMCPClient

# anyio arrives only transitively (via langchain-mcp-adapters/mcp/httpx); guard the import so a
# resolver/lockfile change that drops it degrades the transient-error classifier to "no match"
# rather than crashing at startup.
try:
    import anyio

    _BrokenResourceError: type[BaseException] | None = anyio.BrokenResourceError
except ImportError:  # pragma: no cover - anyio is present in the pinned lockfile
    _BrokenResourceError = None

from automation.agent.toolkits import BaseToolkit

from .conf import settings
from .connections import build_connections_and_filters

if TYPE_CHECKING:
    from langchain_core.tools.base import BaseTool

    from automation.agent.mcp.schemas import ToolFilter

logger = logging.getLogger("daiv.tools")


def _get_connection_url(conn) -> str:
    return getattr(conn, "url", "unknown")


def _is_transient_mcp_error(exc: BaseException) -> bool:
    """True for anticipated, transient MCP-server failures that warrant the same soft treatment as a
    timeout — warn without a traceback so an external outage (e.g. an upstream 503) doesn't mint a
    Sentry error event on every agent run. Covers:

    * ``httpx.HTTPStatusError`` with a 5xx response — an upstream server error, not a config bug.
    * ``anyio.BrokenResourceError`` (matched only when anyio is importable — it's a transitive
      dependency) — the in-memory stream broke, typically the downstream symptom of that same upstream
      failure (the MCP client's writer task died and the reader's ``send()`` finds the stream closed).
    * ``BaseExceptionGroup`` whose leaves are *all* transient — anyio surfaces TaskGroup failures this
      way, so the real exception arrives wrapped. A group mixing in anything non-transient (notably
      ``CancelledError``, a ``BaseException`` that forces a ``BaseExceptionGroup`` ``except Exception``
      never catches anyway) is left for the broad handler so outer cancellation keeps propagating.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    if _BrokenResourceError is not None and isinstance(exc, _BrokenResourceError):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(_is_transient_mcp_error(sub) for sub in exc.exceptions)
    return False


async def _load_server_tools(server_name: str, connection, timeout: float) -> list[BaseTool]:
    """
    Load tools from a single MCP server through its own client. Bounded by ``timeout`` and never raises:
    a hang (e.g. a broken handshake) or any error degrades to an empty tool list. Callers fan this out
    per server so one slow/broken endpoint can neither freeze nor blank tools from healthy peers.
    """
    client = MultiServerMCPClient({server_name: connection}, tool_name_prefix=True)
    try:
        return await asyncio.wait_for(client.get_tools(), timeout=timeout)
    except TimeoutError:
        # Anticipated degradation (server didn't answer within the timeout) — warning, no traceback.
        logger.warning("Timed out loading tools from MCP server %r after %ss; skipping it", server_name, timeout)
        return []
    except Exception as exc:
        # Catch Exception, never BaseException: CancelledError (a BaseException) must propagate so outer
        # cancellation/shutdown isn't swallowed.
        if _is_transient_mcp_error(exc):
            # Anticipated degradation (upstream 5xx / broken MCP stream, possibly wrapped in an
            # ExceptionGroup by anyio's TaskGroup teardown) — warning, no traceback, same treatment as a
            # timeout. Keeps an external outage off Sentry (see DAIV-22/23).
            logger.warning(
                "Transient error loading tools from MCP server %r (%s): %s; skipping it",
                server_name,
                _get_connection_url(connection),
                type(exc).__name__,
            )
        else:
            # logger.exception logs at error level with the traceback, setting unexpected failures apart
            # from the routine timeouts/transient outages above.
            logger.exception(
                "Error getting tools from MCP server %r (%s); skipping it", server_name, _get_connection_url(connection)
            )
        return []


class MCPToolkit(BaseToolkit):
    @classmethod
    async def get_tools(cls, user_id: int | None = None) -> list[BaseTool]:
        from asgiref.sync import sync_to_async
        from mcp_servers.services import build_runtime_servers

        servers = await sync_to_async(build_runtime_servers)(user_id)
        connections, tool_filters = build_connections_and_filters(servers)

        if not connections:
            return []

        server_urls = {name: _get_connection_url(conn) for name, conn in connections.items()}
        logger.debug("Connecting to MCP servers: %s", server_urls)

        # Load each server independently (own client, bounded by a timeout) so one slow/broken/hanging
        # endpoint can neither freeze nor blank tools from healthy peers.
        per_server = await asyncio.gather(
            *(_load_server_tools(name, conn, settings.TOOL_LOAD_TIMEOUT) for name, conn in connections.items())
        )
        tools = [tool for server_tools in per_server for tool in server_tools]

        if tool_filters:
            _warn_on_broken_tool_prefix(connections, per_server, tool_filters)
            tools = _apply_tool_filters(tools, tool_filters)

        for tool in tools:
            tool.handle_tool_error = True
            tool.handle_validation_error = True
            tool.tags = ["mcp_server"]
            tool.metadata = {"mcp_server": tool.name}

        return tools


def _warn_on_broken_tool_prefix(
    connections: dict, per_server: list[list[BaseTool]], filters: dict[str, ToolFilter]
) -> None:
    """Detect a regressed ``tool_name_prefix`` contract before filtering.

    A filtered server that returned tools *none* of which carry its ``"{server}_"`` prefix means the
    prefix scheme broke — its allow/block rules would silently no-op, a fail-open on a security
    boundary (e.g. a read-only allow-list suddenly exposing mutating tools). Checked here, where each
    server's own tool list is still separate, so a server that merely returned nothing (down, timed
    out, or genuinely empty) never trips it — unlike a check over the flattened list, which can't
    tell "no tools" from "unprefixed tools".
    """
    for (server_name, _conn), server_tools in zip(connections.items(), per_server, strict=False):
        if server_name not in filters or not server_tools:
            continue
        prefix = f"{server_name}_"
        if not any(tool.name.startswith(prefix) for tool in server_tools):
            logger.error(
                "MCP server %r returned tools, none carrying the expected %r prefix; its tool filter was "
                "not applied (tool_name_prefix contract regressed — allow/block silently became a no-op)",
                server_name,
                prefix,
            )


def _apply_tool_filters(tools: list[BaseTool], filters: dict[str, ToolFilter]) -> list[BaseTool]:
    """Apply per-server allow/block filters.

    MCP tool names are server-prefixed (e.g. ``sentry_find_organizations``)
    because ``_load_server_tools`` builds each client with
    ``tool_name_prefix=True``. This strips the ``"{server_name}_"`` prefix to
    match a tool against its server's ``tool_filter.items`` — so this filter
    silently depends on that flag staying set. A tool whose name matches no
    configured filter prefix is passed through unchanged.
    """
    filtered = []
    for tool in tools:
        matched = False
        for server_name, tool_filter in filters.items():
            prefix = f"{server_name}_"
            if not tool.name.startswith(prefix):
                continue
            matched = True
            base_name = tool.name[len(prefix) :]
            if tool_filter.allows(base_name):
                filtered.append(tool)
            break

        if not matched:
            filtered.append(tool)

    return filtered
