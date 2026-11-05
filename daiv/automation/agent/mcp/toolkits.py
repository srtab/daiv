from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from .client import build_client, list_tools
from .conf import settings
from .embedded_files import save_embedded_files
from .errors import SOFT_FAILURES, FailureKind, classify

if TYPE_CHECKING:
    from collections.abc import Sequence

    from deepagents.backends.protocol import BackendProtocol
    from langchain_core.tools.base import BaseTool

    from automation.agent.mcp.schemas import UserMcpServer

logger = logging.getLogger("daiv.tools")


async def _load_server_tools(name: str, server: UserMcpServer, backend: BackendProtocol) -> list[BaseTool]:
    """Load, filter and prefix one server's tools, which save the files their results embed under ``backend``.

    Bounded by ``TOOL_LOAD_TIMEOUT``; no ``Exception`` escapes: a hang or any error degrades to an empty list so one
    endpoint can neither freeze nor blank its peers.
    """
    try:
        tools = await asyncio.wait_for(
            list_tools(build_client(server.type, server.url, server.headers)), timeout=settings.TOOL_LOAD_TIMEOUT
        )
    except Exception as exc:  # never BaseException: CancelledError must propagate
        failure = classify(exc)
        if failure.kinds <= SOFT_FAILURES:
            detail = (
                f"timed out after {settings.TOOL_LOAD_TIMEOUT:g}s"
                if failure.kinds == {FailureKind.TIMEOUT}
                else failure.message
            )
            logger.warning("Failed to load tools from MCP server %r (%s): %s; skipping it", name, server.url, detail)
        else:
            logger.exception("Error getting tools from MCP server %r (%s); skipping it", name, server.url)
        return []

    if server.tool_filter is not None:
        tools = [tool for tool in tools if server.tool_filter.allows(tool.name)]
    for tool in tools:
        tool.name = f"{name}_{tool.name}"
        tool.tags = ["mcp_server"]
        tool.metadata = {**(tool.metadata or {}), "mcp_server": name}
        save_embedded_files(tool, backend)
    return tools


async def load_mcp_tools(servers: Sequence[tuple[str, UserMcpServer]], backend: BackendProtocol) -> list[BaseTool]:
    """Load the tools of every ``(name, server)`` pair concurrently; a server that fails to load contributes none."""
    if not servers:
        return []

    logger.debug("Connecting to MCP servers: %s", {name: server.url for name, server in servers})
    per_server = await asyncio.gather(*(_load_server_tools(name, server, backend) for name, server in servers))
    return [tool for server_tools in per_server for tool in server_tools]
