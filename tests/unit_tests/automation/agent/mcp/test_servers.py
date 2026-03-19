from unittest.mock import patch

from automation.agent.mcp.servers import Context7MCPServer, SentryMCPServer


class TestSentryMCPServer:
    def test_sentry_server_has_correct_name(self):
        server = SentryMCPServer()
        assert server.name == "sentry"

    @patch("automation.agent.mcp.servers.settings")
    def test_sentry_server_is_enabled_when_url_set(self, mock_settings):
        mock_settings.SENTRY_URL = "http://mcp-sentry:8000/sse"
        server = SentryMCPServer()

        assert server.is_enabled() is True

    @patch("automation.agent.mcp.servers.settings")
    def test_sentry_server_is_disabled_when_url_none(self, mock_settings):
        mock_settings.SENTRY_URL = None
        server = SentryMCPServer()

        assert server.is_enabled() is False

    def test_sentry_server_tool_filter(self):
        server = SentryMCPServer()

        assert server.tool_filter is not None
        assert server.tool_filter.mode == "allow"
        assert "find_organizations" in server.tool_filter.items
        assert "search_issues" in server.tool_filter.items

    @patch("automation.agent.mcp.servers.settings")
    def test_sentry_get_connection_returns_correct_url(self, mock_settings):
        mock_settings.SENTRY_URL = "http://mcp-sentry:8000/sse"
        server = SentryMCPServer()
        connection = server.get_connection()

        assert connection["transport"] == "sse"
        assert connection["url"] == "http://mcp-sentry:8000/sse"


class TestContext7MCPServer:
    def test_context7_server_has_correct_name(self):
        server = Context7MCPServer()
        assert server.name == "context7"

    @patch("automation.agent.mcp.servers.settings")
    def test_context7_server_is_enabled_when_url_set(self, mock_settings):
        mock_settings.CONTEXT7_URL = "http://mcp-context7:8000/sse"
        server = Context7MCPServer()

        assert server.is_enabled() is True

    @patch("automation.agent.mcp.servers.settings")
    def test_context7_server_is_disabled_when_url_none(self, mock_settings):
        mock_settings.CONTEXT7_URL = None
        server = Context7MCPServer()

        assert server.is_enabled() is False

    def test_context7_server_tool_filter(self):
        server = Context7MCPServer()

        assert server.tool_filter is not None
        assert server.tool_filter.mode == "allow"
        assert "resolve-library-id" in server.tool_filter.items
        assert "query-docs" in server.tool_filter.items

    @patch("automation.agent.mcp.servers.settings")
    def test_context7_get_connection_returns_correct_url(self, mock_settings):
        mock_settings.CONTEXT7_URL = "http://mcp-context7:8000/sse"
        server = Context7MCPServer()
        connection = server.get_connection()

        assert connection["transport"] == "sse"
        assert connection["url"] == "http://mcp-context7:8000/sse"
