from unittest.mock import MagicMock, patch

from automation.tools.mcp.base import MCPServer
from automation.tools.mcp.decorator import mcp_server
from automation.tools.mcp.registry import MCPRegistry


class TestMCPServer(MCPServer):
    name = "test"
    connection = None

    def is_enabled(self) -> bool:
        return True


def test_mcp_server_decorator_registers_class():
    """Test that the decorator registers the class in the registry."""
    # Create a mock registry
    mock_registry = MagicMock(spec=MCPRegistry)

    # Patch the registry to use our mock
    with patch("automation.tools.mcp.decorator.mcp_registry", mock_registry):
        # Create a test MCP server class
        mcp_server(TestMCPServer)
        # The decorator should have registered the class
        mock_registry.register.assert_called_once_with(TestMCPServer)
