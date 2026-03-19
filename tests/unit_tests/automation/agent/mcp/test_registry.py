import json
from unittest.mock import patch

import pytest
from langchain_mcp_adapters.sessions import SSEConnection

from automation.agent.mcp.base import MCPServer
from automation.agent.mcp.registry import MCPRegistry, _expand_env_vars
from automation.agent.mcp.schemas import ToolFilter


class EnabledTestMCPServer(MCPServer):
    """Test MCP server for testing purposes."""

    name = "test_server"
    tool_filter = ToolFilter(mode="allow", items=["tool_a"])

    def is_enabled(self) -> bool:
        return True

    def get_connection(self) -> SSEConnection:
        return SSEConnection(transport="sse", url="http://test-host:8000/sse")


class DisabledTestMCPServer(MCPServer):
    """Disabled test MCP server for testing purposes."""

    name = "disabled_test_server"

    def is_enabled(self) -> bool:
        return False

    def get_connection(self) -> SSEConnection:
        return SSEConnection(transport="sse", url="http://disabled:8000/sse")


class AnotherTestMCPServer(MCPServer):
    """Another test MCP server for testing purposes."""

    name = "another_test_server"

    def is_enabled(self) -> bool:
        return True

    def get_connection(self) -> SSEConnection:
        return SSEConnection(transport="sse", url="http://another:8000/sse")


class TestMCPRegistry:
    @pytest.fixture
    def registry(self):
        """Create a fresh registry for each test."""
        return MCPRegistry()

    def test_init_creates_empty_registry(self, registry):
        assert registry._registry == []

    def test_register_valid_mcp_server(self, registry):
        registry.register(EnabledTestMCPServer)

        assert EnabledTestMCPServer in registry._registry
        assert len(registry._registry) == 1

    def test_register_multiple_servers(self, registry):
        registry.register(EnabledTestMCPServer)
        registry.register(AnotherTestMCPServer)

        assert len(registry._registry) == 2

    def test_register_duplicate_server_raises_assertion_error(self, registry):
        registry.register(EnabledTestMCPServer)

        with pytest.raises(AssertionError, match="is already registered"):
            registry.register(EnabledTestMCPServer)

    def test_register_non_class_raises_assertion_error(self, registry):
        with pytest.raises(AssertionError, match="must be a class that inherits from MCPServer"):
            registry.register("not_a_class")

    def test_register_non_mcp_server_subclass_raises_assertion_error(self, registry):
        class InvalidServer:
            pass

        with pytest.raises(AssertionError, match="must be a class that inherits from MCPServer"):
            registry.register(InvalidServer)

    def test_get_connections_empty_registry(self, registry):
        connections, filters = registry.get_connections_and_filters()
        assert connections == {}
        assert filters == {}

    def test_get_connections_with_enabled_servers(self, registry):
        registry.register(EnabledTestMCPServer)
        registry.register(AnotherTestMCPServer)

        connections, _ = registry.get_connections_and_filters()

        assert len(connections) == 2
        assert "test_server" in connections
        assert "another_test_server" in connections

    def test_get_connections_excludes_disabled_servers(self, registry):
        registry.register(EnabledTestMCPServer)
        registry.register(DisabledTestMCPServer)

        connections, _ = registry.get_connections_and_filters()

        assert len(connections) == 1
        assert "test_server" in connections
        assert "disabled_test_server" not in connections

    def test_get_tool_filters(self, registry):
        registry.register(EnabledTestMCPServer)
        registry.register(AnotherTestMCPServer)

        _, filters = registry.get_connections_and_filters()

        assert len(filters) == 1
        assert "test_server" in filters
        assert filters["test_server"].mode == "allow"
        assert filters["test_server"].items == ["tool_a"]

    def test_get_tool_filters_excludes_disabled_servers(self, registry):
        registry.register(EnabledTestMCPServer)
        registry.register(DisabledTestMCPServer)

        _, filters = registry.get_connections_and_filters()

        assert "disabled_test_server" not in filters

    def test_registry_preserves_order(self, registry):
        registry.register(EnabledTestMCPServer)
        registry.register(AnotherTestMCPServer)

        assert registry._registry[0] == EnabledTestMCPServer
        assert registry._registry[1] == AnotherTestMCPServer


class TestLoadUserServers:
    @pytest.fixture
    def registry(self):
        return MCPRegistry()

    @patch("automation.agent.mcp.registry.settings")
    def test_no_config_file(self, mock_settings, registry):
        mock_settings.SERVERS_CONFIG_FILE = None

        connections = registry._load_user_servers()
        assert connections == {}

    @patch("automation.agent.mcp.registry.settings")
    def test_missing_config_file(self, mock_settings, registry):
        mock_settings.SERVERS_CONFIG_FILE = "/nonexistent/path.json"

        connections = registry._load_user_servers()
        assert connections == {}

    @patch("automation.agent.mcp.registry.settings")
    def test_invalid_json(self, mock_settings, registry, tmp_path):
        config_file = tmp_path / "bad.json"
        config_file.write_text("not json")
        mock_settings.SERVERS_CONFIG_FILE = str(config_file)

        connections = registry._load_user_servers()
        assert connections == {}

    @patch("automation.agent.mcp.registry.settings")
    def test_valid_sse_server(self, mock_settings, registry, tmp_path):
        config_file = tmp_path / "mcp.json"
        config_file.write_text(json.dumps({"mcpServers": {"my-api": {"type": "sse", "url": "http://host:8080/sse"}}}))
        mock_settings.SERVERS_CONFIG_FILE = str(config_file)

        connections = registry._load_user_servers()

        assert len(connections) == 1
        assert "my-api" in connections
        assert connections["my-api"]["transport"] == "sse"
        assert connections["my-api"]["url"] == "http://host:8080/sse"

    @patch("automation.agent.mcp.registry.settings")
    def test_valid_http_server(self, mock_settings, registry, tmp_path):
        config_file = tmp_path / "mcp.json"
        config_file.write_text(json.dumps({"mcpServers": {"my-api": {"type": "http", "url": "http://host:9000/mcp"}}}))
        mock_settings.SERVERS_CONFIG_FILE = str(config_file)

        connections = registry._load_user_servers()

        assert len(connections) == 1
        assert "my-api" in connections
        assert connections["my-api"]["transport"] == "streamable_http"

    @patch("automation.agent.mcp.registry.settings")
    def test_env_var_expansion(self, mock_settings, registry, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "secret123")
        config_file = tmp_path / "mcp.json"
        config_file.write_text(
            json.dumps({
                "mcpServers": {
                    "my-api": {
                        "type": "sse",
                        "url": "http://host:8080/sse",
                        "headers": {"Authorization": "Bearer ${MY_TOKEN}"},
                    }
                }
            })
        )
        mock_settings.SERVERS_CONFIG_FILE = str(config_file)

        connections = registry._load_user_servers()

        assert connections["my-api"]["headers"] == {"Authorization": "Bearer secret123"}

    @patch("automation.agent.mcp.registry.settings")
    def test_env_var_expansion_with_default(self, mock_settings, registry, tmp_path, monkeypatch):
        monkeypatch.delenv("MISSING_VAR", raising=False)
        config_file = tmp_path / "mcp.json"
        config_file.write_text(
            json.dumps({
                "mcpServers": {"my-api": {"type": "sse", "url": "http://${MISSING_VAR:-fallback-host}:8080/sse"}}
            })
        )
        mock_settings.SERVERS_CONFIG_FILE = str(config_file)

        connections = registry._load_user_servers()

        assert connections["my-api"]["url"] == "http://fallback-host:8080/sse"

    @patch("automation.agent.mcp.registry.settings")
    def test_get_connections_merges_builtin_and_user(self, mock_settings, tmp_path):
        """Test that get_connections_and_filters merges built-in and user-defined servers."""
        config_file = tmp_path / "mcp.json"
        config_file.write_text(json.dumps({"mcpServers": {"user-api": {"type": "sse", "url": "http://user:8080/sse"}}}))
        mock_settings.SERVERS_CONFIG_FILE = str(config_file)

        registry = MCPRegistry()
        registry.register(EnabledTestMCPServer)

        connections, filters = registry.get_connections_and_filters()

        assert "test_server" in connections
        assert "user-api" in connections
        assert len(connections) == 2
        assert "test_server" in filters


class TestExpandEnvVars:
    def test_simple_var(self, monkeypatch):
        monkeypatch.setenv("FOO", "bar")
        assert _expand_env_vars("${FOO}") == "bar"

    def test_var_with_default(self, monkeypatch):
        monkeypatch.delenv("MISSING", raising=False)
        assert _expand_env_vars("${MISSING:-default_val}") == "default_val"

    def test_var_with_default_when_set(self, monkeypatch):
        monkeypatch.setenv("PRESENT", "actual")
        assert _expand_env_vars("${PRESENT:-default_val}") == "actual"

    def test_missing_var_kept_as_is(self, monkeypatch):
        monkeypatch.delenv("UNDEFINED", raising=False)
        assert _expand_env_vars("${UNDEFINED}") == "${UNDEFINED}"

    def test_no_vars(self):
        assert _expand_env_vars("plain text") == "plain text"

    def test_multiple_vars(self, monkeypatch):
        monkeypatch.setenv("A", "1")
        monkeypatch.setenv("B", "2")
        assert _expand_env_vars("${A}-${B}") == "1-2"


def test_mcp_registry_singleton():
    """Test that the mcp_registry module-level instance exists."""
    from automation.agent.mcp.registry import mcp_registry

    assert isinstance(mcp_registry, MCPRegistry)
