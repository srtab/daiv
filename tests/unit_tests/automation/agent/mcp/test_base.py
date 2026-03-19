import pytest
from langchain_mcp_adapters.sessions import SSEConnection

from automation.agent.mcp.base import MCPServer
from automation.agent.mcp.schemas import ToolFilter


class ConcreteMCPServer(MCPServer):
    """Concrete implementation of MCPServer for testing purposes."""

    name = "concrete_server"

    def get_connection(self) -> SSEConnection:
        return SSEConnection(transport="sse", url="http://test-host:8000/sse")


class FilteredMCPServer(MCPServer):
    """MCP server with tool filter for testing."""

    name = "filtered_server"
    tool_filter = ToolFilter(mode="allow", items=["tool_a", "tool_b"])

    def get_connection(self) -> SSEConnection:
        return SSEConnection(transport="sse", url="http://test-host:8000/sse")


class CustomEnabledMCPServer(MCPServer):
    """Custom MCP server with overridden is_enabled method."""

    name = "custom_server"

    def is_enabled(self) -> bool:
        return False

    def get_connection(self) -> SSEConnection:
        return SSEConnection(transport="sse", url="http://test-host:8000/sse")


class TestMCPServer:
    def test_mcp_server_has_required_attributes(self):
        """Test that MCPServer has the required class attributes."""
        assert hasattr(MCPServer, "__annotations__")
        assert "name" in MCPServer.__annotations__
        assert MCPServer.__annotations__["name"] is str

    def test_concrete_server_can_be_instantiated(self):
        """Test that a concrete implementation can be instantiated."""
        server = ConcreteMCPServer()

        assert isinstance(server, MCPServer)
        assert server.name == "concrete_server"

    def test_default_is_enabled_returns_true(self):
        """Test that the default is_enabled method returns True."""
        server = ConcreteMCPServer()

        assert server.is_enabled() is True

    def test_is_enabled_can_be_overridden(self):
        """Test that the is_enabled method can be overridden in subclasses."""
        server = CustomEnabledMCPServer()

        assert server.is_enabled() is False

    def test_get_connection_is_abstract(self):
        """Test that MCPServer cannot be instantiated directly (abstract method)."""
        with pytest.raises(TypeError):
            MCPServer()

    def test_get_connection_returns_connection(self):
        """Test that get_connection returns a properly configured connection."""
        server = ConcreteMCPServer()
        connection = server.get_connection()

        assert connection["transport"] == "sse"
        assert connection["url"] == "http://test-host:8000/sse"

    def test_default_tool_filter_is_none(self):
        """Test that the default tool_filter is None."""
        server = ConcreteMCPServer()

        assert server.tool_filter is None

    def test_tool_filter_can_be_set(self):
        """Test that tool_filter can be set on a subclass."""
        server = FilteredMCPServer()

        assert server.tool_filter is not None
        assert server.tool_filter.mode == "allow"
        assert server.tool_filter.items == ["tool_a", "tool_b"]
