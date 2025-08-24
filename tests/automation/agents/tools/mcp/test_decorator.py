from unittest.mock import MagicMock, patch

from automation.agents.tools.mcp.base import MCPServer
from automation.agents.tools.mcp.decorator import mcp_server
from automation.agents.tools.mcp.registry import MCPRegistry
from automation.agents.tools.mcp.schemas import CommonOptions, StdioMcpServer


class TestMCPServer(MCPServer):
    name = "test"
    proxy_config = StdioMcpServer(
        command="test-command", options=CommonOptions(panic_if_invalid=False, log_enabled=True)
    )

    def is_enabled(self) -> bool:
        return True


def test_mcp_server_decorator_registers_class():
    """Test that the decorator registers the class in the registry."""
    # Create a mock registry
    mock_registry = MagicMock(spec=MCPRegistry)

    # Patch the registry to use our mock
    with patch("automation.agents.tools.mcp.decorator.mcp_registry", mock_registry):
        # Create a test MCP server class
        decorated_class = mcp_server(TestMCPServer)
        # The decorator should have registered the class
        mock_registry.register.assert_called_once_with(TestMCPServer)
        # The decorator should return the original class wrapped
        assert decorated_class is not None


def test_mcp_server_decorator_returns_wrapper():
    """Test that the decorator returns a wrapper function that creates instances correctly."""
    # Create a mock registry
    mock_registry = MagicMock(spec=MCPRegistry)

    # Patch the registry to use our mock
    with patch("automation.agents.tools.mcp.decorator.mcp_registry", mock_registry):
        # Apply the decorator
        decorated_class = mcp_server(TestMCPServer)

        # Create an instance using the decorated class
        instance = decorated_class()

        # The instance should be of the original type
        assert isinstance(instance, TestMCPServer)
        assert instance.name == "test"
        assert instance.is_enabled() is True
