from unittest.mock import patch

from automation.tools.mcp.servers import FetchMCPServer, SentryMCPServer


class TestFetchMCPServer:
    def test_fetch_server_has_sse_connection(self):
        """Test that FetchMCPServer has the correct SSEConnection configuration."""
        connection = FetchMCPServer.connection

        assert connection["transport"] == "sse"
        assert connection["url"] == "http://mcp-proxy:9090/fetch/sse"

    @patch("automation.tools.mcp.servers.settings")
    def test_fetch_server_is_enabled_when_setting_true(self, mock_settings):
        """Test that FetchMCPServer is enabled when FETCH_ENABLED is True."""
        mock_settings.FETCH_ENABLED = True
        server = FetchMCPServer()

        assert server.is_enabled() is True

    @patch("automation.tools.mcp.servers.settings")
    def test_fetch_server_is_disabled_when_setting_false(self, mock_settings):
        """Test that FetchMCPServer is disabled when FETCH_ENABLED is False."""
        mock_settings.FETCH_ENABLED = False
        server = FetchMCPServer()

        assert server.is_enabled() is False


class TestSentryMCPServer:
    def test_sentry_server_has_sse_connection(self):
        """Test that SentryMCPServer has the correct SSEConnection configuration."""
        connection = SentryMCPServer.connection

        assert connection["transport"] == "sse"
        assert connection["url"] == "http://mcp-proxy:9090/sentry/sse"

    @patch("automation.tools.mcp.servers.settings")
    def test_sentry_server_is_enabled_when_setting_true(self, mock_settings):
        """Test that SentryMCPServer is enabled when SENTRY_ENABLED is True."""
        mock_settings.SENTRY_ENABLED = True
        server = SentryMCPServer()

        assert server.is_enabled() is True

    @patch("automation.tools.mcp.servers.settings")
    def test_sentry_server_is_disabled_when_setting_false(self, mock_settings):
        """Test that SentryMCPServer is disabled when SENTRY_ENABLED is False."""
        mock_settings.SENTRY_ENABLED = False
        server = SentryMCPServer()

        assert server.is_enabled() is False
