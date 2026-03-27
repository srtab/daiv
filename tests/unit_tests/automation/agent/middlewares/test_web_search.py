from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from automation.agent.middlewares.web_search import WebSearchMiddleware, web_search_tool


class TestWebSearchTool:
    @patch("automation.agent.middlewares.web_search.settings")
    @patch("automation.agent.middlewares.web_search.DuckDuckGoSearchAPIWrapper")
    async def test_successful_search_duckduckgo(self, mock_wrapper_class, mock_settings):
        # Configure settings
        mock_settings.WEB_SEARCH_ENGINE = "duckduckgo"
        mock_settings.WEB_SEARCH_MAX_RESULTS = 5

        # Mock search results
        mock_results = [{"snippet": "Test content", "title": "Test title", "link": "https://example.com"}]
        mock_wrapper = MagicMock()
        mock_wrapper.results.return_value = mock_results
        mock_wrapper_class.return_value = mock_wrapper

        result = await web_search_tool.ainvoke({"query": "test query"})

        assert "Test title" in result
        assert "Test content" in result
        assert "https://example.com" in result
        mock_wrapper.results.assert_called_once_with("test query", max_results=5)

    @patch("automation.agent.middlewares.web_search.settings")
    @patch("automation.agent.middlewares.web_search.TavilySearchAPIWrapper")
    async def test_successful_search_tavily(self, mock_wrapper_class, mock_settings):
        # Configure settings
        mock_settings.WEB_SEARCH_ENGINE = "tavily"
        mock_settings.WEB_SEARCH_MAX_RESULTS = 5

        # Mock search results
        mock_results = {
            "answer": "Test tavily answer",
            "results": [{"content": "Test tavily content", "title": "Test tavily title", "url": "https://example.com"}],
        }
        mock_wrapper = MagicMock()
        mock_wrapper.raw_results_async = AsyncMock(return_value=mock_results)
        mock_wrapper_class.return_value = mock_wrapper

        result = await web_search_tool.ainvoke({"query": "test query"})

        assert "Test tavily answer" in result
        assert "Test tavily content" in result
        assert "Test tavily title" in result
        assert "https://example.com" in result
        mock_wrapper.raw_results_async.assert_called_once_with("test query", max_results=5, include_answer=True)

    @patch("automation.agent.middlewares.web_search.settings")
    @patch("automation.agent.middlewares.web_search.DuckDuckGoSearchAPIWrapper")
    async def test_no_results_duckduckgo(self, mock_wrapper_class, mock_settings):
        # Configure settings
        mock_settings.WEB_SEARCH_ENGINE = "duckduckgo"
        mock_settings.WEB_SEARCH_MAX_RESULTS = 5

        # Mock empty search results
        mock_wrapper = MagicMock()
        mock_wrapper.results.return_value = []
        mock_wrapper_class.return_value = mock_wrapper

        result = await web_search_tool.ainvoke({"query": "test query"})

        assert "No relevant results found" in result
        mock_wrapper.results.assert_called_once_with("test query", max_results=5)

    @patch("automation.agent.middlewares.web_search.settings")
    @patch("automation.agent.middlewares.web_search.TavilySearchAPIWrapper")
    async def test_no_results_tavily(self, mock_wrapper_class, mock_settings):
        # Configure settings
        mock_settings.WEB_SEARCH_ENGINE = "tavily"
        mock_settings.WEB_SEARCH_MAX_RESULTS = 5

        # Mock empty search results
        mock_wrapper = MagicMock()
        mock_wrapper.raw_results_async = AsyncMock(return_value={"answer": None, "results": []})
        mock_wrapper_class.return_value = mock_wrapper

        result = await web_search_tool.ainvoke({"query": "test query"})

        assert "No relevant results found" in result

    @patch("automation.agent.middlewares.web_search.settings")
    async def test_invalid_search_engine(self, mock_settings):
        # Configure settings with invalid engine
        mock_settings.WEB_SEARCH_ENGINE = "invalid_engine"

        try:
            await web_search_tool.ainvoke({"query": "test query"})
            raise AssertionError("Should have raised ValueError")
        except ValueError as e:
            assert "Invalid web search engine: invalid_engine" in str(e)

    @patch("automation.agent.middlewares.web_search.settings")
    @patch("automation.agent.middlewares.web_search.DuckDuckGoSearchAPIWrapper")
    async def test_multiple_results_formatting(self, mock_wrapper_class, mock_settings):
        # Configure settings
        mock_settings.WEB_SEARCH_ENGINE = "duckduckgo"
        mock_settings.WEB_SEARCH_MAX_RESULTS = 5

        # Mock multiple search results
        mock_results = [
            {"snippet": "First result", "title": "First title", "link": "https://example.com/first"},
            {"snippet": "Second result", "title": "Second title", "link": "https://example.com/second"},
            {"snippet": "Third result", "title": "Third title", "link": "https://example.com/third"},
        ]
        mock_wrapper = MagicMock()
        mock_wrapper.results.return_value = mock_results
        mock_wrapper_class.return_value = mock_wrapper

        result = await web_search_tool.ainvoke({"query": "test query"})

        # Check all results are included and properly formatted
        assert "First title" in result
        assert "First result" in result
        assert "https://example.com/first" in result
        assert "Second title" in result
        assert "Second result" in result
        assert "https://example.com/second" in result
        assert "Third title" in result
        assert "Third result" in result
        assert "https://example.com/third" in result
        assert result.count("<web_search_result") == 3
        assert result.count("</web_search_result>") == 3
        mock_wrapper.results.assert_called_once_with("test query", max_results=5)


class TestWebSearchMiddleware:
    @patch("automation.agent.middlewares.web_search.datetime")
    async def test_system_prompt_contains_current_year(self, mock_datetime):
        mock_datetime.now.return_value = datetime(2026, 3, 14)

        middleware = WebSearchMiddleware()
        request = MagicMock()
        request.system_prompt = "Base prompt"
        request.override.return_value = request
        handler = AsyncMock()

        await middleware.awrap_model_call(request, handler)

        call_kwargs = request.override.call_args
        system_prompt = call_kwargs.kwargs["system_prompt"]
        assert "2026" in system_prompt
        assert "2025" in system_prompt  # previous year in the "NOT" example
        assert "2024" not in system_prompt

    @patch("automation.agent.middlewares.web_search.datetime")
    async def test_system_prompt_does_not_contain_hardcoded_year(self, mock_datetime):
        mock_datetime.now.return_value = datetime(2030, 1, 1)

        middleware = WebSearchMiddleware()
        request = MagicMock()
        request.system_prompt = "Base prompt"
        request.override.return_value = request
        handler = AsyncMock()

        await middleware.awrap_model_call(request, handler)

        call_kwargs = request.override.call_args
        system_prompt = call_kwargs.kwargs["system_prompt"]
        assert "2030" in system_prompt
        assert "2029" in system_prompt
        # Should not contain any other hardcoded years
        assert "2025" not in system_prompt
        assert "2026" not in system_prompt

    @patch("automation.agent.middlewares.web_search.datetime")
    async def test_system_prompt_appended_to_base_prompt(self, mock_datetime):
        mock_datetime.now.return_value = datetime(2026, 6, 15)

        middleware = WebSearchMiddleware()
        request = MagicMock()
        request.system_prompt = "Base prompt"
        request.override.return_value = request
        handler = AsyncMock()

        await middleware.awrap_model_call(request, handler)

        call_kwargs = request.override.call_args
        system_prompt = call_kwargs.kwargs["system_prompt"]
        assert system_prompt.startswith("Base prompt\n\n")
        assert "## Web Search tool" in system_prompt

    @patch("automation.agent.middlewares.web_search.datetime")
    async def test_handler_called_with_overridden_request(self, mock_datetime):
        mock_datetime.now.return_value = datetime(2026, 3, 14)

        middleware = WebSearchMiddleware()
        request = MagicMock()
        overridden_request = MagicMock()
        request.override.return_value = overridden_request
        handler = AsyncMock()

        await middleware.awrap_model_call(request, handler)

        handler.assert_awaited_once_with(overridden_request)
