from unittest.mock import AsyncMock, MagicMock, patch

from automation.tools.web_search import WebSearchTool


class TestWebSearchTool:
    @patch("automation.tools.web_search.settings")
    @patch("automation.tools.web_search.DuckDuckGoSearchAPIWrapper")
    async def test_successful_search_duckduckgo(self, mock_wrapper_class, mock_settings):
        # Configure settings
        mock_settings.WEB_SEARCH_ENGINE = "duckduckgo"
        mock_settings.WEB_SEARCH_MAX_RESULTS = 5

        # Mock search results
        mock_results = [{"snippet": "Test content"}]
        mock_wrapper = MagicMock()
        mock_wrapper.results.return_value = mock_results
        mock_wrapper_class.return_value = mock_wrapper

        tool = WebSearchTool()
        result = await tool._arun(query="test query", intent="Testing")

        assert "Test content" in result
        mock_wrapper.results.assert_called_once_with("test query", max_results=5)

    @patch("automation.tools.web_search.settings")
    @patch("automation.tools.web_search.TavilySearchAPIWrapper")
    async def test_successful_search_tavily(self, mock_wrapper_class, mock_settings):
        # Configure settings
        mock_settings.WEB_SEARCH_ENGINE = "tavily"
        mock_settings.WEB_SEARCH_MAX_RESULTS = 5

        # Mock search results
        mock_results = {"answer": "Test tavily answer", "results": [{"content": "Test tavily content"}]}
        mock_wrapper = MagicMock()
        mock_wrapper.raw_results_async = AsyncMock(return_value=mock_results)
        mock_wrapper_class.return_value = mock_wrapper

        tool = WebSearchTool()
        result = await tool._arun(query="test query", intent="Testing")

        assert "Test tavily answer" in result
        assert "Test tavily content" in result
        mock_wrapper.raw_results_async.assert_called_once_with("test query", max_results=5, include_answer=True)

    @patch("automation.tools.web_search.settings")
    @patch("automation.tools.web_search.DuckDuckGoSearchAPIWrapper")
    async def test_no_results_duckduckgo(self, mock_wrapper_class, mock_settings):
        # Configure settings
        mock_settings.WEB_SEARCH_ENGINE = "duckduckgo"
        mock_settings.WEB_SEARCH_MAX_RESULTS = 5

        # Mock empty search results
        mock_wrapper = MagicMock()
        mock_wrapper.results.return_value = []
        mock_wrapper_class.return_value = mock_wrapper

        tool = WebSearchTool()
        result = await tool._arun(query="test query", intent="Testing")

        assert "No relevant results found" in result
        mock_wrapper.results.assert_called_once_with("test query", max_results=5)

    @patch("automation.tools.web_search.settings")
    @patch("automation.tools.web_search.TavilySearchAPIWrapper")
    async def test_no_results_tavily(self, mock_wrapper_class, mock_settings):
        # Configure settings
        mock_settings.WEB_SEARCH_ENGINE = "tavily"
        mock_settings.WEB_SEARCH_MAX_RESULTS = 5

        # Mock empty search results
        mock_wrapper = MagicMock()
        mock_wrapper.raw_results_async = AsyncMock(return_value={"answer": None, "results": []})
        mock_wrapper_class.return_value = mock_wrapper

        tool = WebSearchTool()
        result = await tool._arun(query="test query", intent="Testing")

        assert "No relevant results found" in result

    @patch("automation.tools.web_search.settings")
    async def test_invalid_search_engine(self, mock_settings):
        # Configure settings with invalid engine
        mock_settings.WEB_SEARCH_ENGINE = "invalid_engine"

        tool = WebSearchTool()
        try:
            await tool._arun(query="test query", intent="Testing")
            raise AssertionError("Should have raised ValueError")
        except ValueError as e:
            assert "Invalid web search engine: invalid_engine" in str(e)

    @patch("automation.tools.web_search.settings")
    @patch("automation.tools.web_search.DuckDuckGoSearchAPIWrapper")
    async def test_multiple_results_formatting(self, mock_wrapper_class, mock_settings):
        # Configure settings
        mock_settings.WEB_SEARCH_ENGINE = "duckduckgo"
        mock_settings.WEB_SEARCH_MAX_RESULTS = 5

        # Mock multiple search results
        mock_results = [{"snippet": "First result"}, {"snippet": "Second result"}, {"snippet": "Third result"}]
        mock_wrapper = MagicMock()
        mock_wrapper.results.return_value = mock_results
        mock_wrapper_class.return_value = mock_wrapper

        tool = WebSearchTool()
        result = await tool._arun(query="test query", intent="Testing")

        # Check all results are included and properly formatted
        assert "First result" in result
        assert "Second result" in result
        assert "Third result" in result
        assert result.count("<SearchResult>") == 3
        assert result.count("</SearchResult>") == 3
        mock_wrapper.results.assert_called_once_with("test query", max_results=5)
