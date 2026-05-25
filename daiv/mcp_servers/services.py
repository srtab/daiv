from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import SSEConnection, StreamableHttpConnection

from automation.agent.mcp.schemas import UserMcpServer
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
        out.append((row.name, UserMcpServer(type=row.transport, url=row.url, headers=headers or None)))
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
    except Exception as err:  # noqa: BLE001 — surface any failure to the UI
        return {"ok": False, "error": str(err)}
    return {"ok": True, "tools": [{"name": t.name, "description": getattr(t, "description", "")} for t in tools]}


async def discover_tools(server: MCPServer) -> list[dict[str, str]]:
    """Discover the tools exposed by a saved server. Headers are decrypted
    and env-refs resolved before the handshake; on failure, returns an empty
    list (the caller already shows the row, just without tool browser data)."""
    payload = {"transport": server.transport, "url": server.url, "headers": server.headers or []}
    result = await test_connection(payload)
    if not result.get("ok"):
        logger.warning("Tool discovery failed for MCP server '%s': %s", server.name, result.get("error"))
        return []
    return result["tools"]
