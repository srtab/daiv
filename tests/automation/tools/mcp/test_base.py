from unittest.mock import patch

from automation.tools.mcp.base import MCPServer
from automation.tools.mcp.schemas import CommonOptions, StdioMcpServer


class ConcreteMCPServer(MCPServer):
    """Concrete implementation of MCPServer for testing purposes."""

    name = "concrete_server"
    proxy_config = StdioMcpServer(
        command="test-command", args=["arg1", "arg2"], options=CommonOptions(panic_if_invalid=False, log_enabled=True)
    )


class CustomEnabledMCPServer(MCPServer):
    """Custom MCP server with overridden is_enabled method."""

    name = "custom_server"
    proxy_config = StdioMcpServer(
        command="test-command", options=CommonOptions(panic_if_invalid=False, log_enabled=True)
    )

    def is_enabled(self) -> bool:
        return False


class TestMCPServer:
    def test_mcp_server_has_required_attributes(self):
        """Test that MCPServer has the required class attributes."""
        # These should be defined as type annotations
        assert hasattr(MCPServer, "__annotations__")
        assert "name" in MCPServer.__annotations__
        assert "proxy_config" in MCPServer.__annotations__
        assert MCPServer.__annotations__["name"] is str

    def test_concrete_server_can_be_instantiated(self):
        """Test that a concrete implementation can be instantiated."""
        server = ConcreteMCPServer()

        assert isinstance(server, MCPServer)
        assert server.name == "concrete_server"
        assert server.proxy_config is not None

    def test_default_is_enabled_returns_true(self):
        """Test that the default is_enabled method returns True."""
        server = ConcreteMCPServer()

        assert server.is_enabled() is True

    def test_is_enabled_can_be_overridden(self):
        """Test that the is_enabled method can be overridden in subclasses."""
        server = CustomEnabledMCPServer()

        assert server.is_enabled() is False

    @patch("automation.tools.mcp.base.settings")
    def test_get_connection_returns_sse_connection(self, mock_settings):
        """Test that get_connection returns a properly configured SSEConnection."""
        mock_settings.MCP_PROXY_HOST.encoded_string.return_value = "http://test-host:9090"

        server = ConcreteMCPServer()
        connection = server.get_connection()

        assert connection["transport"] == "sse"
        assert "concrete_server/sse" in connection["url"]

    def test_get_proxy_config_returns_configured_proxy(self):
        """Test that get_proxy_config returns the configured proxy configuration."""
        server = ConcreteMCPServer()
        proxy_config = server.get_proxy_config()

        assert proxy_config is server.proxy_config
        assert isinstance(proxy_config, StdioMcpServer)
        assert proxy_config.command == "test-command"
