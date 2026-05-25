from __future__ import annotations

import logging

from automation.agent.mcp.schemas import UserMcpServer
from mcp_servers.models import MCPServer

logger = logging.getLogger("daiv.mcp_servers")


def build_runtime_servers() -> list[tuple[str, UserMcpServer]]:
    """Read enabled ``MCPServer`` rows from the DB and convert each to the
    ``UserMcpServer`` DTO the registry consumes. Returns a list of
    ``(name, dto)`` tuples preserving DB ordering.

    Raises nothing for individual-row failures: a bad row is skipped with a
    warning so other servers still load. Errors in the DB layer itself
    propagate to the caller (``MCPToolkit.get_tools`` already swallows them).
    """
    rows = MCPServer.objects.filter(enabled=True).order_by("name")
    out: list[tuple[str, UserMcpServer]] = []
    for row in rows:
        headers = _resolve_headers(row)
        out.append((row.name, UserMcpServer(type=row.transport, url=row.url, headers=headers or None)))
    return out


def _resolve_headers(row: MCPServer) -> dict[str, str]:
    """Flatten the structured ``[{name, mode, value}]`` shape into the DTO's
    ``dict[str, str]``. Literal values come through directly; env_ref values
    are resolved via ``os.environ``."""
    import os

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
