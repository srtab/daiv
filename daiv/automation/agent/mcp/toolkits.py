from __future__ import annotations

import logging
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Any

from langchain_mcp_adapters.tools import load_mcp_tools
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from automation.agent.toolkits import BaseToolkit

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from langchain_core.tools.base import BaseTool

    from automation.agent.mcp.schemas import ToolFilter

logger = logging.getLogger("daiv.tools")


def _get_connection_url(conn) -> str:
    return getattr(conn, "url", "unknown")


@asynccontextmanager
async def _open_streamable_mcp_session(
    *, url: str, headers: dict[str, Any] | None, terminate_on_close: bool, initialize: bool
) -> AsyncIterator[tuple[ClientSession, Callable[[], str | None]]]:
    """Open a single Streamable-HTTP MCP session and expose the server-issued session id.

    ``langchain_mcp_adapters``' helper discards the ``get_session_id`` callback;
    we re-implement it so callers can persist the id and resume on a later turn.
    Set ``initialize=False`` when reconnecting to a session the server already
    knows — the Playwright server rejects re-init with -32600 "Server already
    initialized" otherwise.
    """
    async with (
        streamablehttp_client(url, headers=headers, terminate_on_close=terminate_on_close) as (read, write, get_id),
        ClientSession(read, write) as session,
    ):
        if initialize:
            await session.initialize()
        yield session, get_id


class MCPToolkit(BaseToolkit):
    @classmethod
    @asynccontextmanager
    async def aopen(
        cls, *, session_ids: dict[str, str] | None = None
    ) -> AsyncIterator[tuple[list[BaseTool], dict[str, str]]]:
        """Open one persistent MCP session per registered server for the block's scope.

        Tools yielded inside the ``async with`` reuse their server's session on
        every invocation. Stateful MCP servers (Playwright above all) need this:
        each MCP session gets its own browser context, so without session reuse
        ``navigate`` and ``snapshot`` land in different browsers and the second
        sees ``about:blank``.

        ``session_ids`` controls cross-turn behaviour:

        - ``None`` (default): sessions terminate on context exit; each call gets
          fresh state. Right for one-shot runs (jobs, webhooks).
        - ``dict``: persistent mode. For each server with an id in the dict, we
          reconnect to that server-side session (skipping init); for servers
          without one, we open fresh and capture the id back into the dict. The
          MCP session stays alive past close so the next call with the same dict
          can resume it. Right for chat threads.

        On a stale id (404 "Session not found"), we transparently fall back to
        a fresh session and overwrite the id in ``session_ids`` so the caller's
        next persistence write doesn't preserve the dead value. A server that
        fails to open is logged and skipped; the rest still connect.
        """
        from automation.agent.mcp.registry import mcp_registry

        connections, tool_filters = mcp_registry.get_connections_and_filters()
        if not connections:
            yield [], {}
            return

        server_urls = {name: _get_connection_url(conn) for name, conn in connections.items()}
        logger.debug("Opening MCP sessions: %s", server_urls)

        persist = session_ids is not None
        ids_out: dict[str, str] = dict(session_ids) if session_ids else {}

        async with AsyncExitStack() as stack:
            tools: list[BaseTool] = []
            for name, conn in connections.items():
                if conn.get("transport") != "streamable_http":
                    logger.warning("MCP %s: non-streamable transports are not supported by aopen", name)
                    continue
                url = conn["url"]
                base_headers = dict(conn.get("headers") or {})
                existing_id = ids_out.get(name)
                server_tools, captured_id = await _open_server(stack, url, base_headers, persist, existing_id, name)
                if server_tools is None:
                    if existing_id and name in ids_out:
                        # Drop the stale id so the caller doesn't persist a dead one.
                        del ids_out[name]
                    continue
                tools.extend(server_tools)
                if persist:
                    new_id = captured_id or existing_id
                    if new_id:
                        ids_out[name] = new_id
                    elif name in ids_out:
                        # Server doesn't issue session ids (stateless); drop the empty slot.
                        del ids_out[name]

            if tool_filters:
                tools = _apply_tool_filters(tools, tool_filters)

            for tool in tools:
                tool.handle_tool_error = True
                tool.handle_validation_error = True
                tool.tags = ["mcp_server"]
                tool.metadata = {"mcp_server": tool.name}

            yield tools, ids_out


async def _open_server(
    stack: AsyncExitStack, url: str, base_headers: dict[str, Any], persist: bool, existing_id: str | None, name: str
) -> tuple[list[BaseTool] | None, str | None]:
    """Open one MCP server, attempting resume when an existing id is supplied.

    Returns ``(tools, captured_id)`` on success, ``(None, None)`` on failure.
    A stale ``existing_id`` is recovered from automatically by opening fresh.
    """
    terminate_on_close = not persist

    if existing_id:
        try:
            session, get_id = await stack.enter_async_context(
                _open_streamable_mcp_session(
                    url=url,
                    headers={**base_headers, "Mcp-Session-Id": existing_id},
                    terminate_on_close=terminate_on_close,
                    initialize=False,
                )
            )
            tools = await load_mcp_tools(session, server_name=name, tool_name_prefix=True)
            return tools, get_id() or existing_id
        except Exception:
            logger.warning("MCP %s: resume of session %s failed; opening fresh", name, existing_id, exc_info=True)

    try:
        session, get_id = await stack.enter_async_context(
            _open_streamable_mcp_session(
                url=url, headers=base_headers or None, terminate_on_close=terminate_on_close, initialize=True
            )
        )
        tools = await load_mcp_tools(session, server_name=name, tool_name_prefix=True)
    except Exception:
        logger.warning("MCP %s: open failed", name, exc_info=True)
        return None, None
    return tools, get_id()


def _apply_tool_filters(tools: list[BaseTool], filters: dict[str, ToolFilter]) -> list[BaseTool]:
    """
    Apply tool filters from MCP server configurations.

    Tools from MCP servers are prefixed with the server name (e.g., "sentry_find_organizations").
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
            if (tool_filter.mode == "allow" and base_name in tool_filter.items) or (
                tool_filter.mode == "block" and base_name not in tool_filter.items
            ):
                filtered.append(tool)
            break

        if not matched:
            filtered.append(tool)

    return filtered
