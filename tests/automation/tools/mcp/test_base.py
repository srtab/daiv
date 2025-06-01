from unittest.mock import Mock

from langchain_mcp_adapters.sessions import SSEConnection

from automation.tools.mcp.base import MCPServer


class ConcreteMCPServer(MCPServer):
    """Concrete implementation of MCPServer for testing purposes."""

    name = "concrete_server"
    connection = Mock(spec=SSEConnection)


class CustomEnabledMCPServer(MCPServer):
    """Custom MCP server with overridden is_enabled method."""

    name = "custom_server"
    connection = Mock(spec=SSEConnection)

    def is_enabled(self) -> bool:
        return False


class TestMCPServer:
    def test_mcp_server_has_required_attributes(self):
        """Test that MCPServer has the required class attributes."""
        # These should be defined as type annotations
        assert hasattr(MCPServer, "__annotations__")
        assert "name" in MCPServer.__annotations__
        assert "connection" in MCPServer.__annotations__
        assert MCPServer.__annotations__["name"] is str
        assert MCPServer.__annotations__["connection"] is SSEConnection

    def test_concrete_server_can_be_instantiated(self):
        """Test that a concrete implementation can be instantiated."""
        server = ConcreteMCPServer()

        assert isinstance(server, MCPServer)
        assert server.name == "concrete_server"
        assert server.connection is not None

    def test_default_is_enabled_returns_true(self):
        """Test that the default is_enabled method returns True."""
        server = ConcreteMCPServer()

        assert server.is_enabled() is True

    def test_is_enabled_can_be_overridden(self):
        """Test that the is_enabled method can be overridden in subclasses."""
        server = CustomEnabledMCPServer()

        assert server.is_enabled() is False
