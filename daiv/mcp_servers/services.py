from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, TypedDict

from django.core.cache import cache

from asgiref.sync import async_to_sync
from langchain_mcp_adapters.client import MultiServerMCPClient

from automation.agent.mcp.connections import build_connection
from automation.agent.mcp.schemas import ToolFilter, UserMcpServer
from core.encryption import DecryptionError
from mcp_servers.constants import TOOLS_CACHE_KEY, TOOLS_CACHE_TIMEOUT, TOOLS_NEGATIVE_CACHE_TIMEOUT
from mcp_servers.models import MCPServer

logger = logging.getLogger("daiv.mcp_servers")

_TEST_CONNECTION_TIMEOUT = 5.0  # seconds


class HeaderEntry(TypedDict, total=False):
    """Stored shape of a single header in ``MCPServer.headers``.

    ``mode`` is ``"literal"`` (``value`` used verbatim) or ``"env_ref"``
    (``value`` is the name of an env var resolved at runtime).
    """

    name: str
    mode: str
    value: str


def build_runtime_servers() -> list[tuple[str, UserMcpServer]]:
    """Read all enabled ``MCPServer`` rows from the DB (built-in and custom
    alike) and convert each to the ``UserMcpServer`` DTO the toolkit consumes.
    Returns a list of ``(name, dto)`` tuples preserving DB ordering.

    A row whose ``headers`` cannot be decrypted is skipped with an error log;
    other rows still load. A failure of the DB query itself propagates to the
    caller: ``MCPToolkit.get_tools`` does not guard it, so a DB outage surfaces
    as a failed agent-graph build rather than as silently-empty tools.
    """
    rows = MCPServer.objects.filter(enabled=True).order_by("name")
    out: list[tuple[str, UserMcpServer]] = []
    for row in rows:
        try:
            headers = _resolve_header_entries(row.headers or [], server_name=row.name)
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


def _resolve_header_entries(entries: list[HeaderEntry] | None, *, server_name: str) -> dict[str, str]:
    """Flatten the stored ``[{name, mode, value}]`` header shape into the
    DTO's ``dict[str, str]``.

    Literal values pass through directly; ``env_ref`` values are resolved via
    ``os.environ``. A missing env var drops that one header (logged) without
    affecting others. An unrecognized ``mode`` also drops the header with a
    warning, so a typo can't silently vanish a header.

    Shared by ``build_runtime_servers`` (runtime) and ``_build_client``
    (test-connection) so resolution and logging cannot drift between them.
    """
    resolved: dict[str, str] = {}
    for entry in entries or []:
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
                    server_name,
                    name,
                    value,
                )
                continue
            resolved[name] = env_value
        else:
            logger.warning(
                "MCP server '%s' header '%s' has unrecognized mode %r; dropping header", server_name, name, mode
            )
    return resolved


def _build_client(payload: dict[str, Any]) -> MultiServerMCPClient:
    """Build a transient ``MultiServerMCPClient`` from a form-shaped payload.

    ``payload`` is ``{"transport": "http"|"sse", "url": str, "headers":
    [{"name", "mode", "value"}, ...]}``. ``mode=env_ref`` values are resolved
    against ``os.environ``; missing ones are dropped (see
    ``_resolve_header_entries``).
    """
    url = payload.get("url")
    resolved = _resolve_header_entries(payload.get("headers"), server_name=url or "test-connection")
    transport = payload.get("transport")
    connection = build_connection(transport, url, resolved or None)
    if connection is None:
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
    look enabled but whose headers ``build_runtime_servers`` would skip
    (undecryptable) or partially drop (missing env-ref var)."""
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
    failure. Propagates :class:`core.encryption.DecryptionError` so views can
    surface a key-rotation error instead of 500-ing.
    """
    payload = {"transport": server.transport, "url": server.url, "headers": server.headers or []}
    result = await test_connection(payload)
    if not result.get("ok"):
        logger.warning("Tool discovery failed for MCP server '%s': %s", server.name, result.get("error"))
        return []
    return result["tools"]


def discover_tools_cached(server: MCPServer) -> list[dict[str, str]]:
    """Cache-backed, exception-safe wrapper around :func:`discover_tools` for
    the edit view.

    Degrades to ``[]`` on :class:`DecryptionError` (key rotation) so the edit
    form (its sole caller) never 500s on a key-rotated server. A successful
    discovery is cached for
    ``TOOLS_CACHE_TIMEOUT``; an empty/unreachable result is cached only for the
    shorter ``TOOLS_NEGATIVE_CACHE_TIMEOUT`` so a transient failure is neither
    pinned for the full TTL nor re-probed (a 5s handshake) on every render.
    """
    stamp = int(server.modified.timestamp())
    cache_key = TOOLS_CACHE_KEY.format(name=server.name, stamp=stamp)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        tools = async_to_sync(discover_tools)(server)
    except DecryptionError:
        logger.warning("Cannot discover tools for %r: header decryption failed.", server.name)
        return []
    cache.set(cache_key, tools, TOOLS_CACHE_TIMEOUT if tools else TOOLS_NEGATIVE_CACHE_TIMEOUT)
    return tools
