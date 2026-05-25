from unittest.mock import patch

from automation.agent.mcp.servers import Context7MCPServer, PlaywrightMCPServer, SentryMCPServer


class TestSentryMCPServer:
    def test_sentry_server_has_correct_name(self):
        server = SentryMCPServer()
        assert server.name == "sentry"

    @patch("automation.agent.mcp.servers.settings")
    def test_sentry_server_is_enabled_when_url_set(self, mock_settings):
        mock_settings.SENTRY_URL = "http://mcp-sentry:8000/mcp"
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
        assert "search_issue" in server.tool_filter.items

    @patch("automation.agent.mcp.servers.settings")
    def test_sentry_get_connection_returns_correct_url(self, mock_settings):
        mock_settings.SENTRY_URL = "http://mcp-sentry:8000/mcp"
        server = SentryMCPServer()
        connection = server.get_connection()

        assert connection["transport"] == "streamable_http"
        assert connection["url"] == "http://mcp-sentry:8000/mcp"


class TestContext7MCPServer:
    def test_context7_server_has_correct_name(self):
        server = Context7MCPServer()
        assert server.name == "context7"

    @patch("automation.agent.mcp.servers.settings")
    def test_context7_server_is_enabled_when_url_set(self, mock_settings):
        mock_settings.CONTEXT7_URL = "http://mcp-context7:8000/mcp"
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
        mock_settings.CONTEXT7_URL = "http://mcp-context7:8000/mcp"
        server = Context7MCPServer()
        connection = server.get_connection()

        assert connection["transport"] == "streamable_http"
        assert connection["url"] == "http://mcp-context7:8000/mcp"


class TestPlaywrightMCPServer:
    def test_playwright_server_has_correct_name(self):
        server = PlaywrightMCPServer()
        assert server.name == "playwright"

    @patch("automation.agent.mcp.servers.settings")
    def test_playwright_server_is_enabled_when_url_set(self, mock_settings):
        mock_settings.PLAYWRIGHT_URL = "http://mcp_playwright:8931/mcp"
        server = PlaywrightMCPServer()

        assert server.is_enabled() is True

    @patch("automation.agent.mcp.servers.settings")
    def test_playwright_server_is_disabled_when_url_none(self, mock_settings):
        mock_settings.PLAYWRIGHT_URL = None
        server = PlaywrightMCPServer()

        assert server.is_enabled() is False

    def test_playwright_server_has_no_tool_filter(self):
        """Documents the 'full Playwright surface' decision as a test assertion.

        A future change that adds a filter has to update this test, making the
        deviation visible in the diff.
        """
        server = PlaywrightMCPServer()

        assert server.tool_filter is None

    @patch("automation.agent.mcp.servers.settings")
    def test_playwright_get_connection_returns_correct_url(self, mock_settings):
        mock_settings.PLAYWRIGHT_URL = "http://mcp_playwright:8931/mcp"
        server = PlaywrightMCPServer()
        connection = server.get_connection()

        assert connection["transport"] == "streamable_http"
        assert connection["url"] == "http://mcp_playwright:8931/mcp"
