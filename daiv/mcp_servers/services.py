from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import SSEConnection, StreamableHttpConnection

from automation.agent.mcp.schemas import ToolFilter, UserMcpServer
from core.encryption import DecryptionError
from mcp_servers.models import MCPServer

logger = logging.getLogger("daiv.mcp_servers")

_TEST_CONNECTION_TIMEOUT = 5.0  # seconds


def build_runtime_servers() -> list[tuple[str, UserMcpServer]]:
    """Read enabled ``MCPServer`` rows from the DB and convert each to the
    ``UserMcpServer`` DTO the registry consumes. Returns a list of
    ``(name, dto)`` tuples preserving DB ordering.

    A row whose ``headers`` cannot be decrypted is skipped with an error log;
    other rows still load. Errors in the DB layer itself propagate to the
    caller (``MCPToolkit.get_tools`` already swallows them).
    """
    rows = MCPServer.objects.filter(enabled=True, source=MCPServer.Source.CUSTOM).order_by("name")
    out: list[tuple[str, UserMcpServer]] = []
    for row in rows:
        try:
            headers = _resolve_headers(row)
        except DecryptionError:
            logger.exception("MCP server '%s' (pk=%s) header decryption failed; skipping", row.name, row.pk)
            continue
        tool_filter = None
        if row.tool_filter_mode != MCPServer.FilterMode.NONE and row.tool_filter_items:
            tool_filter = ToolFilter(mode=row.tool_filter_mode, items=list(row.tool_filter_items))

        out.append((
            row.name,
            UserMcpServer(type=row.transport, url=row.url, headers=headers or None, tool_filter=tool_filter),
        ))
    return out


def _resolve_headers(row: MCPServer) -> dict[str, str]:
    """Flatten the structured ``[{name, mode, value}]`` shape into the DTO's
    ``dict[str, str]``. Literal values come through directly; env_ref values
    are resolved via ``os.environ``. A missing env var drops that one header
    but does not affect others."""
    headers = row.headers or []
    resolved: dict[str, str] = {}
    for entry in headers:
        name = entry.get("name")
        mode = entry.get("mode")
        value = entry.get("value", "")
        if not name:
            continue
        if mode == "literal":
            resolved[name] = value
        elif mode == "env_ref":
            env_value = os.environ.get(value)
            if env_value is None:
                logger.warning(
                    "MCP server '%s' header '%s' references missing env var '%s'; dropping header",
                    row.name,
                    name,
                    value,
                )
                continue
            resolved[name] = env_value
    return resolved


def _build_client(payload: dict[str, Any]) -> MultiServerMCPClient:
    """Build a transient ``MultiServerMCPClient`` from a form-shaped payload.

    ``payload`` is ``{"transport": "http"|"sse", "url": str, "headers":
    [{"name", "mode", "value"}, ...]}``. ``mode=env_ref`` values are
    resolved against ``os.environ``; missing ones are dropped.
    """
    resolved: dict[str, str] = {}
    for entry in payload.get("headers", []) or []:
        name = entry.get("name")
        mode = entry.get("mode")
        value = entry.get("value", "")
        if not name:
            continue
        if mode == "literal":
            resolved[name] = value
        elif mode == "env_ref":
            env_value = os.environ.get(value)
            if env_value is not None:
                resolved[name] = env_value

    headers = resolved or None
    transport = payload.get("transport")
    url = payload.get("url")
    if transport == "http":
        connection = StreamableHttpConnection(transport="streamable_http", url=url, headers=headers)
    elif transport == "sse":
        connection = SSEConnection(transport="sse", url=url, headers=headers)
    else:
        raise ValueError(f"Unsupported transport: {transport!r}")

    return MultiServerMCPClient({"__probe__": connection})


async def test_connection(payload: dict[str, Any]) -> dict[str, Any]:
    """Open a transient MCP session against ``payload`` and return either
    ``{ok: True, tools: [...]}`` or ``{ok: False, error: ...}``."""
    try:
        client = _build_client(payload)
        tools = await asyncio.wait_for(client.get_tools(), timeout=_TEST_CONNECTION_TIMEOUT)
    except TimeoutError:
        logger.warning("MCP test_connection timed out for url=%s", payload.get("url"))
        return {"ok": False, "error": f"Connection timed out after {_TEST_CONNECTION_TIMEOUT:g}s"}
    except Exception as err:  # noqa: BLE001 — surface any failure to the UI
        logger.exception("MCP test_connection failed for url=%s", payload.get("url"))
        # str(err) is empty for many httpx/asyncio exceptions; class name keeps the message greppable.
        detail = str(err) or type(err).__name__
        return {"ok": False, "error": f"{type(err).__name__}: {detail}"}
    return {"ok": True, "tools": [{"name": t.name, "description": getattr(t, "description", "")} for t in tools]}


def server_health(server: MCPServer) -> dict[str, Any]:
    """Synchronous decryption + env-ref check, no network. Flags rows that
    look enabled but would be silently skipped by ``build_runtime_servers``."""
    try:
        raw = server.headers or []
    except DecryptionError:
        return {"ok": False, "reason": "headers cannot be decrypted"}
    missing = [
        entry.get("value") or "(empty)"
        for entry in raw
        if entry.get("mode") == "env_ref" and os.environ.get(entry.get("value") or "") is None
    ]
    if missing:
        return {"ok": False, "reason": "missing env var(s): " + ", ".join(missing)}
    return {"ok": True, "reason": None}


async def discover_tools(server: MCPServer) -> list[dict[str, str]]:
    """Discover tools exposed by a saved server. Returns ``[]`` on handshake
    failure. Propagates :class:`core.encryption.DecryptionError` so views
    can surface a key-rotation error instead of 500-ing.
    """
    payload = {"transport": server.transport, "url": server.url, "headers": server.headers or []}
    result = await test_connection(payload)
    if not result.get("ok"):
        logger.warning("Tool discovery failed for MCP server '%s': %s", server.name, result.get("error"))
        return []
    return result["tools"]
