from unittest.mock import MagicMock, patch

from automation.tools.web_search import WebSearchTool


class TestWebSearchTool:
    @patch("automation.tools.web_search.DDGS")
    def test_successful_search(self, mock_ddgs):
        # Mock search results
        mock_results = [{"title": "Test Title", "link": "https://example.com", "body": "Test content"}]
        mock_ddgs_instance = MagicMock()
        mock_ddgs_instance.text.return_value = mock_results
        mock_ddgs.return_value.__enter__.return_value = mock_ddgs_instance

        tool = WebSearchTool()
        result = tool._run(query="test query", intent="Testing")

        assert "Test Title" in result
        assert "https://example.com" in result
        assert "Test content" in result
        mock_ddgs_instance.text.assert_called_once_with("test query", max_results=5)

    @patch("automation.tools.web_search.DDGS")
    def test_no_results(self, mock_ddgs):
        # Mock empty search results
        mock_ddgs_instance = MagicMock()
        mock_ddgs_instance.text.return_value = []
        mock_ddgs.return_value.__enter__.return_value = mock_ddgs_instance

        tool = WebSearchTool()
        result = tool._run(query="test query", intent="Testing")

        assert "No relevant results found" in result
        mock_ddgs_instance.text.assert_called_once_with("test query", max_results=5)
