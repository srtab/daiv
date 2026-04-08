from __future__ import annotations

import json
import logging
import os
import pathlib
import re
from inspect import isclass
from typing import TYPE_CHECKING

from langchain_mcp_adapters.sessions import SSEConnection, StreamableHttpConnection
from pydantic import ValidationError

from .base import MCPServer
from .conf import settings
from .schemas import ToolFilter, UserMcpServersConfig

if TYPE_CHECKING:
    from langchain_mcp_adapters.sessions import Connection


logger = logging.getLogger("daiv.mcp")

_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _expand_env_vars(value: str) -> str:
    """
    Expand environment variables in a string value.

    Supports ${VAR} and ${VAR:-default} syntax.
    """

    def replacer(match: re.Match) -> str:
        expr = match.group(1)
        if ":-" in expr:
            var_name, default = expr.split(":-", 1)
            return os.environ.get(var_name, default)
        result = os.environ.get(expr)
        if result is None:
            logger.warning("Environment variable '%s' referenced in MCP config is not set", expr)
            return match.group(0)
        return result

    return _ENV_VAR_PATTERN.sub(replacer, value)


class MCPRegistry:
    """
    Registry that keeps track of the registered MCP servers.
    """

    def __init__(self):
        self._registry: list[type[MCPServer]] = []

    def register(self, server: type[MCPServer]) -> None:
        assert isclass(server) and issubclass(server, MCPServer), (
            f"{server} must be a class that inherits from MCPServer"
        )
        assert server not in self._registry, f"{server} is already registered as MCP server."

        self._registry.append(server)

    def get_connections_and_filters(self) -> tuple[dict[str, Connection], dict[str, ToolFilter]]:
        """
        Get all connections and tool filters in a single pass over the registry.
        """
        connections: dict[str, Connection] = {}
        filters: dict[str, ToolFilter] = {}

        for server_class in self._registry:
            server = server_class()
            if server.is_enabled():
                connections[server.name] = server.get_connection()
                if server.tool_filter is not None:
                    filters[server.name] = server.tool_filter

        user_connections, user_filters = self._load_user_servers()
        collisions = connections.keys() & user_connections.keys()
        if collisions:
            logger.warning("User-defined MCP server(s) override built-in: %s", collisions)
        connections.update(user_connections)
        filters.update(user_filters)

        return connections, filters

    def _load_user_servers(self) -> tuple[dict[str, Connection], dict[str, ToolFilter]]:
        """
        Load user-defined MCP servers from the JSON config file.
        """
        if not settings.SERVERS_CONFIG_FILE:
            return {}, {}

        try:
            with pathlib.Path(settings.SERVERS_CONFIG_FILE).open() as f:
                raw = json.load(f)
        except FileNotFoundError:
            logger.warning("MCP servers config file not found: %s", settings.SERVERS_CONFIG_FILE)
            return {}, {}
        except OSError:
            logger.exception("Cannot read MCP servers config file: %s", settings.SERVERS_CONFIG_FILE)
            return {}, {}
        except json.JSONDecodeError:
            logger.error("Invalid JSON in MCP servers config file: %s", settings.SERVERS_CONFIG_FILE)
            return {}, {}

        try:
            config = UserMcpServersConfig.model_validate(raw)
        except ValidationError:
            logger.exception("Invalid MCP servers config: %s", settings.SERVERS_CONFIG_FILE)
            return {}, {}

        connections: dict[str, Connection] = {}
        filters: dict[str, ToolFilter] = {}
        for name, server in config.mcp_servers.items():
            url = _expand_env_vars(server.url)
            headers = {k: _expand_env_vars(v) for k, v in server.headers.items()} if server.headers else None

            if server.type == "sse":
                connections[name] = SSEConnection(transport="sse", url=url, headers=headers)
            elif server.type == "http":
                connections[name] = StreamableHttpConnection(transport="streamable_http", url=url, headers=headers)

            if server.tool_filter is not None:
                filters[name] = server.tool_filter

        return connections, filters


mcp_registry = MCPRegistry()
