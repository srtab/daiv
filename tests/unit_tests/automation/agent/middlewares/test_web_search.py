import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from automation.agent.middlewares.web_search import WebSearchMiddleware, web_search_tool


class TestWebSearchTool:
    @patch("automation.agent.middlewares.web_search.site_settings")
    @patch("automation.agent.middlewares.web_search.DuckDuckGoSearchAPIWrapper")
    async def test_successful_search_duckduckgo(self, mock_wrapper_class, mock_settings):
        # Configure settings
        mock_settings.web_search_engine = "duckduckgo"
        mock_settings.web_search_max_results = 5

        mock_results = [{"snippet": "Test content", "title": "Test title", "link": "https://example.com"}]
        mock_wrapper = MagicMock()
        mock_wrapper.results.return_value = mock_results
        mock_wrapper_class.return_value = mock_wrapper

        result = await web_search_tool.ainvoke({"query": "test query"})

        parsed = json.loads(result)
        assert parsed == [{"title": "Test title", "link": "https://example.com", "content": "Test content"}]
        mock_wrapper.results.assert_called_once_with("test query", max_results=5)

    @patch("automation.agent.middlewares.web_search.site_settings")
    @patch("automation.agent.middlewares.web_search.TavilySearchAPIWrapper")
    async def test_successful_search_tavily(self, mock_wrapper_class, mock_settings):
        mock_settings.web_search_engine = "tavily"
        mock_settings.web_search_max_results = 5

        mock_results = {
            "answer": "Test tavily answer",
            "results": [{"content": "Test tavily content", "title": "Test tavily title", "url": "https://example.com"}],
        }
        mock_wrapper = MagicMock()
        mock_wrapper.raw_results_async = AsyncMock(return_value=mock_results)
        mock_wrapper_class.return_value = mock_wrapper

        result = await web_search_tool.ainvoke({"query": "test query"})

        parsed = json.loads(result)
        # Tavily's synthesized answer is prepended with link="" so the model can tell it apart from citable hits.
        assert parsed[0] == {"title": "Suggested answer", "link": "", "content": "Test tavily answer"}
        assert parsed[1] == {
            "title": "Test tavily title",
            "link": "https://example.com",
            "content": "Test tavily content",
        }
        mock_wrapper.raw_results_async.assert_called_once_with("test query", max_results=5, include_answer=True)

    @patch("automation.agent.middlewares.web_search.site_settings")
    @patch("automation.agent.middlewares.web_search.DuckDuckGoSearchAPIWrapper")
    async def test_no_results_duckduckgo(self, mock_wrapper_class, mock_settings):
        mock_settings.web_search_engine = "duckduckgo"
        mock_settings.web_search_max_results = 5

        mock_wrapper = MagicMock()
        mock_wrapper.results.return_value = []
        mock_wrapper_class.return_value = mock_wrapper

        result = await web_search_tool.ainvoke({"query": "test query"})

        assert json.loads(result) == []
        mock_wrapper.results.assert_called_once_with("test query", max_results=5)

    @patch("automation.agent.middlewares.web_search.site_settings")
    @patch("automation.agent.middlewares.web_search.TavilySearchAPIWrapper")
    async def test_no_results_tavily(self, mock_wrapper_class, mock_settings):
        mock_settings.web_search_engine = "tavily"
        mock_settings.web_search_max_results = 5

        mock_wrapper = MagicMock()
        mock_wrapper.raw_results_async = AsyncMock(return_value={"answer": None, "results": []})
        mock_wrapper_class.return_value = mock_wrapper

        result = await web_search_tool.ainvoke({"query": "test query"})

        assert json.loads(result) == []

    @patch("automation.agent.middlewares.web_search.site_settings")
    @patch("automation.agent.middlewares.web_search.DuckDuckGoSearchAPIWrapper")
    async def test_special_chars_survive_json_roundtrip(self, mock_wrapper_class, mock_settings):
        # JSON encoding handles `"`, `&`, etc. natively — guard against future regressions
        # if anyone reintroduces ad-hoc string formatting.
        mock_settings.web_search_engine = "duckduckgo"
        mock_settings.web_search_max_results = 5

        mock_wrapper = MagicMock()
        mock_wrapper.results.return_value = [
            {"title": 'Why "X" beats Y & Z', "link": "https://example.com/q?a=1&b=2", "snippet": "body with <tag>"}
        ]
        mock_wrapper_class.return_value = mock_wrapper

        result = await web_search_tool.ainvoke({"query": "q"})

        parsed = json.loads(result)
        assert parsed[0]["title"] == 'Why "X" beats Y & Z'
        assert parsed[0]["link"] == "https://example.com/q?a=1&b=2"
        assert parsed[0]["content"] == "body with <tag>"

    @patch("automation.agent.middlewares.web_search.site_settings")
    async def test_invalid_search_engine(self, mock_settings):
        mock_settings.web_search_engine = "invalid_engine"

        try:
            await web_search_tool.ainvoke({"query": "test query"})
            raise AssertionError("Should have raised ValueError")
        except ValueError as e:
            assert "Invalid web search engine: invalid_engine" in str(e)

    @patch("automation.agent.middlewares.web_search.site_settings")
    @patch("automation.agent.middlewares.web_search.DuckDuckGoSearchAPIWrapper")
    async def test_multiple_results_formatting(self, mock_wrapper_class, mock_settings):
        mock_settings.web_search_engine = "duckduckgo"
        mock_settings.web_search_max_results = 5

        mock_results = [
            {"snippet": "First result", "title": "First title", "link": "https://example.com/first"},
            {"snippet": "Second result", "title": "Second title", "link": "https://example.com/second"},
            {"snippet": "Third result", "title": "Third title", "link": "https://example.com/third"},
        ]
        mock_wrapper = MagicMock()
        mock_wrapper.results.return_value = mock_results
        mock_wrapper_class.return_value = mock_wrapper

        result = await web_search_tool.ainvoke({"query": "test query"})

        parsed = json.loads(result)
        assert len(parsed) == 3
        assert [r["title"] for r in parsed] == ["First title", "Second title", "Third title"]
        assert [r["link"] for r in parsed] == [
            "https://example.com/first",
            "https://example.com/second",
            "https://example.com/third",
        ]
        assert [r["content"] for r in parsed] == ["First result", "Second result", "Third result"]
        mock_wrapper.results.assert_called_once_with("test query", max_results=5)


class TestWebSearchMiddleware:
    @patch("automation.agent.middlewares.web_search.timezone")
    async def test_system_prompt_contains_current_year(self, mock_timezone):
        mock_timezone.now.return_value = datetime(2026, 3, 14)

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

    @patch("automation.agent.middlewares.web_search.timezone")
    async def test_system_prompt_does_not_contain_hardcoded_year(self, mock_timezone):
        mock_timezone.now.return_value = datetime(2030, 1, 1)

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

    @patch("automation.agent.middlewares.web_search.timezone")
    async def test_system_prompt_appended_to_base_prompt(self, mock_timezone):
        mock_timezone.now.return_value = datetime(2026, 6, 15)

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

    @patch("automation.agent.middlewares.web_search.timezone")
    async def test_handler_called_with_overridden_request(self, mock_timezone):
        mock_timezone.now.return_value = datetime(2026, 3, 14)

        middleware = WebSearchMiddleware()
        request = MagicMock()
        overridden_request = MagicMock()
        request.override.return_value = overridden_request
        handler = AsyncMock()

        await middleware.awrap_model_call(request, handler)

        handler.assert_awaited_once_with(overridden_request)
