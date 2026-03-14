from unittest.mock import patch

from automation.agent.mcp.servers import Context7MCPServer, SentryMCPServer


class TestSentryMCPServer:
    def test_sentry_server_has_correct_name(self):
        """Test that SentryMCPServer has the correct name."""
        server = SentryMCPServer()
        assert server.name == "sentry"

    @patch("automation.agent.mcp.servers.settings")
    def test_sentry_server_is_enabled_when_settings_true_and_token_present(self, mock_settings):
        """Test that SentryMCPServer is enabled when SENTRY_ENABLED is True and token is present."""
        from pydantic import SecretStr

        mock_settings.SENTRY_ENABLED = True
        mock_settings.SENTRY_ACCESS_TOKEN = SecretStr("test-token")
        server = SentryMCPServer()

        assert server.is_enabled() is True

    @patch("automation.agent.mcp.servers.settings")
    def test_sentry_server_is_disabled_when_setting_false(self, mock_settings):
        """Test that SentryMCPServer is disabled when SENTRY_ENABLED is False."""
        from pydantic import SecretStr

        mock_settings.SENTRY_ENABLED = False
        mock_settings.SENTRY_ACCESS_TOKEN = SecretStr("test-token")
        server = SentryMCPServer()

        assert server.is_enabled() is False

    @patch("automation.agent.mcp.servers.settings")
    def test_sentry_server_is_disabled_when_no_token(self, mock_settings):
        """Test that SentryMCPServer is disabled when no access token is provided."""
        mock_settings.SENTRY_ENABLED = True
        mock_settings.SENTRY_ACCESS_TOKEN = None
        server = SentryMCPServer()

        assert server.is_enabled() is False


class TestContext7MCPServer:
    def test_context7_server_has_correct_name(self):
        """Test that Context7MCPServer has the correct name."""
        server = Context7MCPServer()
        assert server.name == "context7"

    @patch("automation.agent.mcp.servers.settings")
    def test_context7_server_is_enabled_when_setting_true(self, mock_settings):
        """Test that Context7MCPServer is enabled when CONTEXT7_ENABLED is True."""
        mock_settings.CONTEXT7_ENABLED = True
        server = Context7MCPServer()

        assert server.is_enabled() is True

    @patch("automation.agent.mcp.servers.settings")
    def test_context7_server_is_disabled_when_setting_false(self, mock_settings):
        """Test that Context7MCPServer is disabled when CONTEXT7_ENABLED is False."""
        mock_settings.CONTEXT7_ENABLED = False
        server = Context7MCPServer()

        assert server.is_enabled() is False

    def test_context7_server_proxy_config_includes_api_key_env(self):
        """Test that Context7MCPServer proxy config includes the CONTEXT7_API_KEY env var."""
        server = Context7MCPServer()
        assert "CONTEXT7_API_KEY" in server.proxy_config.env
