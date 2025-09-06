from unittest.mock import patch

import pytest
from pydantic import ValidationError

from automation.agents.tools.mcp.schemas import McpConfiguration, McpProxyConfig, StdioMcpServer


class TestMcpConfiguration:
    def test_mcp_configuration_validates_server_names(self):
        """Test that server name validation works."""
        proxy_config = McpProxyConfig(
            base_url="http://localhost:9090", addr=":9090", name="test-proxy", version="1.0.0"
        )

        stdio_server = StdioMcpServer(command="test-command")
        servers = {"": stdio_server}  # Empty server name should fail

        with pytest.raises(ValidationError, match="Server names cannot be empty"):
            McpConfiguration(mcp_proxy=proxy_config, mcp_servers=servers)

    def test_mcp_configuration_validates_whitespace_server_names(self):
        """Test that whitespace-only server names are rejected."""
        proxy_config = McpProxyConfig(
            base_url="http://localhost:9090", addr=":9090", name="test-proxy", version="1.0.0"
        )

        stdio_server = StdioMcpServer(command="test-command")
        servers = {"   ": stdio_server}  # Whitespace-only server name should fail

        with pytest.raises(ValidationError, match="Server names cannot be empty"):
            McpConfiguration(mcp_proxy=proxy_config, mcp_servers=servers)

    @patch("automation.agents.tools.mcp.conf.settings")
    @patch("automation.agents.tools.mcp.registry.mcp_registry")
    def test_mcp_configuration_populate_with_auth_token(self, mock_registry, mock_settings):
        """Test McpConfiguration.populate() with auth token."""
        # Mock settings
        mock_settings.PROXY_AUTH_TOKEN.get_secret_value.return_value = "test-token"
        mock_settings.PROXY_HOST.encoded_string.return_value = "http://localhost:9090"
        mock_settings.PROXY_ADDR = ":9090"

        # Mock registry
        mock_registry.get_mcp_servers_config.return_value = {"test_server": StdioMcpServer(command="test-command")}

        config = McpConfiguration.populate()

        assert config.mcp_proxy.base_url == "http://localhost:9090"
        assert config.mcp_proxy.addr == ":9090"
        assert config.mcp_proxy.name == "daiv-mcp-proxy"
        assert config.mcp_proxy.version == "0.1.0"
        assert config.mcp_proxy.options.auth_tokens == ["test-token"]
        assert config.mcp_proxy.options.panic_if_invalid is False
        assert config.mcp_proxy.options.log_enabled is True
        assert "test_server" in config.mcp_servers

    @patch("automation.agents.tools.mcp.conf.settings")
    @patch("automation.agents.tools.mcp.registry.mcp_registry")
    def test_mcp_configuration_populate_without_auth_token(self, mock_registry, mock_settings):
        """Test McpConfiguration.populate() without auth token."""
        # Mock settings
        mock_settings.PROXY_AUTH_TOKEN = None
        mock_settings.PROXY_HOST.encoded_string.return_value = "http://localhost:9090"
        mock_settings.PROXY_ADDR = ":9090"

        # Mock registry
        mock_registry.get_mcp_servers_config.return_value = {}

        config = McpConfiguration.populate()

        assert config.mcp_proxy.options.auth_tokens == []
        assert config.mcp_servers == {}
