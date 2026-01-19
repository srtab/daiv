from unittest.mock import patch

from automation.agent.mcp.servers import SentryMCPServer


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
