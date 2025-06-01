from unittest.mock import Mock

import pytest
from langchain_mcp_adapters.sessions import Connection

from automation.tools.mcp.base import MCPServer
from automation.tools.mcp.registry import MCPRegistry


class TestMCPServer(MCPServer):
    """Test MCP server for testing purposes."""

    name = "test_server"
    connection = Mock(spec=Connection)

    def is_enabled(self) -> bool:
        return True


class DisabledTestMCPServer(MCPServer):
    """Disabled test MCP server for testing purposes."""

    name = "disabled_test_server"
    connection = Mock(spec=Connection)

    def is_enabled(self) -> bool:
        return False


class AnotherTestMCPServer(MCPServer):
    """Another test MCP server for testing purposes."""

    name = "another_test_server"
    connection = Mock(spec=Connection)

    def is_enabled(self) -> bool:
        return True


class TestMCPRegistry:
    @pytest.fixture
    def registry(self):
        """Create a fresh registry for each test."""
        return MCPRegistry()

    def test_init_creates_empty_registry(self, registry):
        """Test that initialization creates an empty registry."""
        assert registry._registry == []

    def test_register_valid_mcp_server(self, registry):
        """Test registering a valid MCP server class."""
        registry.register(TestMCPServer)

        assert TestMCPServer in registry._registry
        assert len(registry._registry) == 1

    def test_register_multiple_servers(self, registry):
        """Test registering multiple MCP server classes."""
        registry.register(TestMCPServer)
        registry.register(AnotherTestMCPServer)

        assert TestMCPServer in registry._registry
        assert AnotherTestMCPServer in registry._registry
        assert len(registry._registry) == 2

    def test_register_duplicate_server_raises_assertion_error(self, registry):
        """Test that registering the same server twice raises AssertionError."""
        registry.register(TestMCPServer)

        with pytest.raises(AssertionError, match="TestMCPServer.*is already registered"):
            registry.register(TestMCPServer)

    def test_register_non_class_raises_assertion_error(self, registry):
        """Test that registering a non-class raises AssertionError."""
        invalid_server = "not_a_class"

        with pytest.raises(AssertionError, match="must be a class that inherits from MCPServer"):
            registry.register(invalid_server)

    def test_register_non_mcp_server_subclass_raises_assertion_error(self, registry):
        """Test that registering a class that doesn't inherit from MCPServer raises AssertionError."""

        class InvalidServer:
            pass

        with pytest.raises(AssertionError, match="must be a class that inherits from MCPServer"):
            registry.register(InvalidServer)

    def test_register_instance_instead_of_class_raises_assertion_error(self, registry):
        """Test that registering an instance instead of a class raises AssertionError."""
        server_instance = TestMCPServer()

        with pytest.raises(AssertionError, match="must be a class that inherits from MCPServer"):
            registry.register(server_instance)

    def test_get_connections_empty_registry(self, registry):
        """Test getting connections from an empty registry."""
        connections = registry.get_connections()

        assert connections == {}

    def test_get_connections_with_enabled_servers(self, registry):
        """Test getting connections from registry with enabled servers."""
        registry.register(TestMCPServer)
        registry.register(AnotherTestMCPServer)

        connections = registry.get_connections()

        assert len(connections) == 2
        assert "test_server" in connections
        assert "another_test_server" in connections
        assert connections["test_server"] == TestMCPServer.connection
        assert connections["another_test_server"] == AnotherTestMCPServer.connection

    def test_get_connections_excludes_disabled_servers(self, registry):
        """Test that get_connections excludes disabled servers."""
        registry.register(TestMCPServer)
        registry.register(DisabledTestMCPServer)

        connections = registry.get_connections()

        assert len(connections) == 1
        assert "test_server" in connections
        assert "disabled_test_server" not in connections

    def test_get_connections_mixed_enabled_disabled_servers(self, registry):
        """Test get_connections with a mix of enabled and disabled servers."""
        registry.register(TestMCPServer)
        registry.register(DisabledTestMCPServer)
        registry.register(AnotherTestMCPServer)

        connections = registry.get_connections()

        assert len(connections) == 2
        assert "test_server" in connections
        assert "another_test_server" in connections
        assert "disabled_test_server" not in connections

    def test_registry_preserves_order(self, registry):
        """Test that the registry preserves the order of registration."""
        registry.register(TestMCPServer)
        registry.register(AnotherTestMCPServer)

        assert registry._registry[0] == TestMCPServer
        assert registry._registry[1] == AnotherTestMCPServer


def test_mcp_registry_singleton():
    """Test that the mcp_registry module-level instance exists."""
    from automation.tools.mcp.registry import mcp_registry

    assert isinstance(mcp_registry, MCPRegistry)
