from __future__ import annotations

import logging
import os

from automation.agent.mcp.schemas import UserMcpServer
from core.encryption import DecryptionError
from mcp_servers.models import MCPServer

logger = logging.getLogger("daiv.mcp_servers")


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
