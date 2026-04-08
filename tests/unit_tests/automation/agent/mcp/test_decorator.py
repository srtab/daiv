from unittest.mock import MagicMock, patch

from langchain_mcp_adapters.sessions import SSEConnection

from automation.agent.mcp.base import MCPServer
from automation.agent.mcp.decorator import mcp_server
from automation.agent.mcp.registry import MCPRegistry


class TestMCPServer(MCPServer):
    name = "test"

    def is_enabled(self) -> bool:
        return True

    def get_connection(self) -> SSEConnection:
        return SSEConnection(transport="sse", url="http://test:8000/sse")


def test_mcp_server_decorator_registers_class():
    """Test that the decorator registers the class in the registry."""
    mock_registry = MagicMock(spec=MCPRegistry)

    with patch("automation.agent.mcp.decorator.mcp_registry", mock_registry):
        decorated_class = mcp_server(TestMCPServer)
        mock_registry.register.assert_called_once_with(TestMCPServer)
        assert decorated_class is TestMCPServer


def test_mcp_server_decorator_returns_original_class():
    """Test that the decorator returns the original class, not a wrapper."""
    mock_registry = MagicMock(spec=MCPRegistry)

    with patch("automation.agent.mcp.decorator.mcp_registry", mock_registry):
        decorated_class = mcp_server(TestMCPServer)

        assert decorated_class is TestMCPServer
        instance = decorated_class()
        assert isinstance(instance, TestMCPServer)
        assert instance.name == "test"
