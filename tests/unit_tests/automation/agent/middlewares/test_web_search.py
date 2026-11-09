import inspect
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import httpx
import pytest
from ddgs import ddgs as ddgs_module
from ddgs.exceptions import DDGSException
from pydantic import SecretStr

from automation.agent.middlewares.web_search import (
    WEB_SEARCH_SYSTEM_PROMPT,
    WebSearchConfigurationError,
    WebSearchMiddleware,
    web_search_tool,
)


@pytest.fixture
def duckduckgo():
    """Patched DuckDuckGo backend; yields the wrapper so a test only states its own behaviour."""
    with (
        patch("automation.agent.middlewares.web_search.site_settings") as settings,
        patch("automation.agent.middlewares.web_search.DuckDuckGoSearchAPIWrapper") as wrapper_class,
    ):
        settings.web_search_engine = "duckduckgo"
        settings.web_search_max_results = 5
        wrapper_class.return_value = wrapper = MagicMock()
        yield wrapper


@pytest.fixture
def searxng():
    with patch("automation.agent.middlewares.web_search.site_settings") as settings:
        settings.web_search_engine = "searxng"
        settings.web_search_searxng_url = "http://searxng:8080/prefix/search"
        settings.web_search_searxng_timeout_seconds = 15
        settings.web_search_max_results = 2
        yield settings


class TestSearXNGSearch:
    async def test_invalid_environment_coercion_is_sanitized(self, searxng, httpx_mock):
        with (
            patch.object(
                type(searxng),
                "web_search_searxng_timeout_seconds",
                new_callable=PropertyMock,
                side_effect=ValueError("raw env secret"),
                create=True,
            ),
            pytest.raises(WebSearchConfigurationError, match="Invalid SearXNG configuration") as error,
        ):
            await web_search_tool.ainvoke({"query": "q"})

        assert "secret" not in str(error.value)
        assert httpx_mock.get_requests() == []

    async def test_dispatch_request_normalization_and_limit(self, searxng, httpx_mock):
        httpx_mock.add_response(
            json={
                "results": [
                    {"title": "Öffentliche API", "url": "https://example.org/a", "content": "Änderung"},
                    {"title": "Second", "url": "https://example.org/b", "content": None},
                    {"title": "Third", "url": "https://example.org/c", "content": "unused"},
                ]
            }
        )

        result = json.loads(await web_search_tool.ainvoke({"query": "a & b/ä? !docs"}))

        assert result == [
            {"title": "Öffentliche API", "link": "https://example.org/a", "content": "Änderung"},
            {"title": "Second", "link": "https://example.org/b", "content": ""},
        ]
        request = httpx_mock.get_request()
        assert request.method == "GET"
        assert request.url.scheme == "http"
        assert request.url.host == "searxng"
        assert request.url.port == 8080
        assert request.url.path == "/prefix/search"
        assert dict(request.url.params) == {"q": "a & b/ä? !docs", "format": "json"}
        assert "authorization" not in request.headers

    @pytest.mark.parametrize(
        "url",
        [
            "",
            None,
            "ftp://example.org/search",
            "http://user:secret@example.org/search",
            "http://example.org/search?secret=key",
            "http://example.org/search#fragment",
        ],
    )
    async def test_invalid_configuration_never_sends_request(self, searxng, httpx_mock, url):
        searxng.web_search_searxng_url = url

        with pytest.raises(WebSearchConfigurationError, match="SearXNG search URL") as error:
            await web_search_tool.ainvoke({"query": "q"})

        assert "secret" not in str(error.value)
        assert httpx_mock.get_requests() == []

    @pytest.mark.parametrize("timeout", [None, 0, -1, 121, 1.5, "15", True])
    async def test_invalid_timeout_never_sends_request(self, searxng, httpx_mock, timeout):
        searxng.web_search_searxng_timeout_seconds = timeout

        with pytest.raises(WebSearchConfigurationError, match="between 1 and 120"):
            await web_search_tool.ainvoke({"query": "q"})

        assert httpx_mock.get_requests() == []

    @pytest.mark.parametrize("maximum", [None, 0, -1, 1.5, "5", True])
    async def test_invalid_result_limit_never_sends_request(self, searxng, httpx_mock, maximum):
        searxng.web_search_max_results = maximum

        with pytest.raises(WebSearchConfigurationError, match="positive integer"):
            await web_search_tool.ainvoke({"query": "q"})

        assert httpx_mock.get_requests() == []

    @pytest.mark.parametrize("timeout", [1, 120])
    async def test_client_lifetime_timeout_and_standard_tls(self, searxng, httpx_mock, monkeypatch, timeout):
        searxng.web_search_searxng_url = "https://search.example.org/search"
        searxng.web_search_searxng_timeout_seconds = timeout
        httpx_mock.add_response(json={"results": []})
        factory = httpx.AsyncClient
        clients = []
        options = []

        def create_client(**kwargs):
            options.append(kwargs)
            client = factory(**kwargs)
            clients.append(client)
            return client

        monkeypatch.setattr("automation.agent.middlewares.web_search.httpx.AsyncClient", create_client)

        assert json.loads(await web_search_tool.ainvoke({"query": "q"})) == []

        assert options == [{"timeout": timeout, "follow_redirects": False}]
        assert clients[0].is_closed
        assert all(value == timeout for value in httpx_mock.get_request().extensions["timeout"].values())

    @pytest.mark.parametrize(
        "status, expected",
        [(401, "authentication"), (403, "Enable JSON"), (429, "rate limit"), (500, "HTTP 500"), (503, "HTTP 503")],
    )
    async def test_http_errors_are_sanitized_tool_results(self, searxng, httpx_mock, caplog, status, expected):
        httpx_mock.add_response(status_code=status, text="upstream-secret-and-private-details")

        result = await web_search_tool.ainvoke({"query": "q"})

        assert result.startswith("error: Web search failed.")
        assert expected in result
        assert "upstream-secret" not in result + caplog.text
        assert "http://searxng" not in result + caplog.text
        record = next(record for record in caplog.records if "SearXNG web search failed" in record.getMessage())
        assert record.levelname == "ERROR"
        assert record.exc_info is None

    async def test_client_is_closed_on_request_failure(self, searxng, httpx_mock, monkeypatch):
        httpx_mock.add_exception(httpx.ReadTimeout("upstream detail"))
        factory = httpx.AsyncClient
        clients = []

        def create_client(**kwargs):
            client = factory(**kwargs)
            clients.append(client)
            return client

        monkeypatch.setattr("automation.agent.middlewares.web_search.httpx.AsyncClient", create_client)

        result = await web_search_tool.ainvoke({"query": "q"})

        assert "timed out" in result
        assert clients[0].is_closed

    async def test_redirect_is_not_followed(self, searxng, httpx_mock):
        httpx_mock.add_response(status_code=302, headers={"Location": "https://private.example.org/?secret=token"})

        result = await web_search_tool.ainvoke({"query": "!external query"})

        assert "redirects are not followed" in result
        assert "private.example" not in result
        assert "secret" not in result
        assert len(httpx_mock.get_requests()) == 1

    @pytest.mark.parametrize(
        "error_type, expected", [(httpx.ReadTimeout, "timed out"), (httpx.ConnectError, "network access")]
    )
    async def test_network_errors_are_sanitized(self, searxng, httpx_mock, caplog, error_type, expected):
        httpx_mock.add_exception(error_type("secret private URL or TLS details"))

        result = await web_search_tool.ainvoke({"query": "q"})

        assert expected in result
        assert "secret" not in result + caplog.text

    async def test_transport_setup_error_is_sanitized(self, searxng, monkeypatch, caplog):
        def fail_client(**kwargs):
            raise ValueError("proxy secret")

        monkeypatch.setattr("automation.agent.middlewares.web_search.httpx.AsyncClient", fail_client)

        result = await web_search_tool.ainvoke({"query": "q"})

        assert "transport configuration failed" in result
        assert "secret" not in result + caplog.text

    async def test_non_json_is_a_backend_failure(self, searxng, httpx_mock):
        httpx_mock.add_response(text="<html>secret sign-in page</html>")

        result = await web_search_tool.ainvoke({"query": "q"})

        assert "invalid JSON" in result
        assert "secret" not in result

    @pytest.mark.parametrize(
        "payload",
        [{}, [], {"results": None}, {"results": {}}, {"results": [], "unresponsive_engines": "private error"}],
    )
    async def test_invalid_payload_is_a_backend_failure(self, searxng, httpx_mock, payload):
        httpx_mock.add_response(json=payload)

        result = await web_search_tool.ainvoke({"query": "q"})

        assert "invalid search response" in result
        assert "private error" not in result

    async def test_empty_success_is_not_an_error(self, searxng, httpx_mock):
        httpx_mock.add_response(json={"results": [], "unresponsive_engines": []})

        assert json.loads(await web_search_tool.ainvoke({"query": "q"})) == []

    async def test_no_results_with_failed_engines_is_an_error(self, searxng, httpx_mock, caplog):
        httpx_mock.add_response(json={"results": [], "unresponsive_engines": [["private-engine", "secret"]]})

        result = await web_search_tool.ainvoke({"query": "q"})

        assert "engine failures" in result
        assert "secret" not in result + caplog.text
        assert "private-engine" not in result + caplog.text

    async def test_partial_results_remain_usable_and_flagged(self, searxng, httpx_mock, caplog):
        httpx_mock.add_response(
            json={
                "results": [{"title": "Result", "url": "https://example.org", "content": "snippet"}],
                "unresponsive_engines": [["private-engine", "secret"]],
            }
        )

        result = json.loads(await web_search_tool.ainvoke({"query": "q"}))

        assert result[0]["title"] == "Result"
        assert result[0]["link"] == "https://example.org"
        assert "Partial search results" in result[0]["content"]
        assert result[0]["content"].endswith("snippet")
        assert "secret" not in json.dumps(result) + caplog.text
        assert any(record.levelname == "ERROR" for record in caplog.records)

    async def test_unusable_rows_are_skipped_before_limiting(self, searxng, httpx_mock):
        httpx_mock.add_response(
            json={
                "results": [
                    None,
                    {"url": "file:///etc/passwd"},
                    {"url": "https://user:secret@example.org"},
                    {"url": "https://example.org/first", "title": None},
                    {"url": "https://example.org/second", "content": "second"},
                    {"url": "https://example.org/third"},
                ]
            }
        )

        result = json.loads(await web_search_tool.ainvoke({"query": "q"}))

        assert [row["link"] for row in result] == ["https://example.org/first", "https://example.org/second"]
        assert result[0]["title"] == "https://example.org/first"
        assert "Partial search results" in result[0]["content"]
        assert "secret" not in json.dumps(result)

    async def test_only_unusable_rows_are_a_backend_failure(self, searxng, httpx_mock):
        httpx_mock.add_response(json={"results": [{"title": "Not a citable result"}]})

        result = await web_search_tool.ainvoke({"query": "q"})

        assert "no usable result entries" in result


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
        # Defensive twin of test_duckduckgo_no_results_is_not_a_backend_failure: the real ddgs
        # backend raises rather than returning [], so only that test covers a reachable route.
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

    # DDGSException derives straight from Exception: an `except OSError` narrowing of the tool's
    # catch-all passes every other test in this file while reintroducing the run-killing abort.
    @pytest.mark.parametrize(
        "raised", [DDGSException("ConnectError: dns error"), ConnectionError("DNS error: no records found")]
    )
    async def test_backend_failure_returns_error_string(self, duckduckgo, raised):
        duckduckgo.results.side_effect = raised

        result = await web_search_tool.ainvoke({"query": "test query"})

        # The repo-wide tool convention: `error:` instead of JSON means the tool itself failed.
        assert result.startswith("error: Web search failed.")
        assert f"{type(raised).__name__}: {raised}" in result

    @patch("automation.agent.middlewares.web_search.site_settings")
    @patch("automation.agent.middlewares.web_search.TavilySearchAPIWrapper")
    async def test_backend_failure_returns_error_string_tavily(self, mock_wrapper_class, mock_settings):
        mock_settings.web_search_engine = "tavily"
        mock_settings.web_search_max_results = 5
        mock_settings.web_search_api_key = SecretStr("key")

        mock_wrapper = MagicMock()
        # TavilySearchAPIWrapper flattens every non-200 into a bare Exception.
        mock_wrapper.raw_results_async = AsyncMock(side_effect=Exception("Error 503: Service Unavailable"))
        mock_wrapper_class.return_value = mock_wrapper

        result = await web_search_tool.ainvoke({"query": "test query"})

        assert result == "error: Web search failed. Details: Exception: Error 503: Service Unavailable"

    async def test_backend_error_detail_is_truncated(self, duckduckgo):
        duckduckgo.results.side_effect = DDGSException("x" * 5000)

        result = await web_search_tool.ainvoke({"query": "test query"})

        assert result.endswith("…[truncated]")
        assert len(result) < 600

    async def test_backend_failure_is_logged_at_error(self, duckduckgo, caplog):
        # Sentry's default LoggingIntegration only turns ERROR into an event, so a warning here
        # would leave a permanently dead backend degrading every run with no operator signal.
        duckduckgo.results.side_effect = DDGSException("ConnectError: dns error")

        with caplog.at_level("WARNING", logger="daiv.tools"):
            await web_search_tool.ainvoke({"query": "test query"})

        record = next(rec for rec in caplog.records if "Web search failed" in rec.getMessage())
        assert record.levelname == "ERROR"
        assert record.exc_info is not None

    async def test_duckduckgo_no_results_is_not_a_backend_failure(self, duckduckgo):
        # ddgs raises instead of returning [] when every engine matched nothing; the model must not
        # be told the backend failed.
        duckduckgo.results.side_effect = DDGSException("No results found.")

        result = await web_search_tool.ainvoke({"query": "test query"})

        assert json.loads(result) == []

    def test_ddgs_no_results_sentinel_is_unchanged(self):
        """Pin the upstream string the no-results mapping keys on, so a ddgs bump fails here
        instead of silently turning an empty search back into a backend failure."""
        source = inspect.getsource(ddgs_module.DDGS._search_sync)

        assert 'raise DDGSException(err or "No results found.")' in source

    @pytest.mark.parametrize("api_key", [None, SecretStr("")])
    @patch("automation.agent.middlewares.web_search.site_settings")
    async def test_unusable_tavily_api_key_propagates(self, mock_settings, api_key):
        mock_settings.web_search_engine = "tavily"
        mock_settings.web_search_api_key = api_key

        with pytest.raises(WebSearchConfigurationError, match="Web search API key is not configured"):
            await web_search_tool.ainvoke({"query": "test query"})

    @patch("automation.agent.middlewares.web_search.site_settings")
    async def test_invalid_search_engine(self, mock_settings):
        mock_settings.web_search_engine = "invalid_engine"

        with pytest.raises(WebSearchConfigurationError, match="Invalid web search engine: invalid_engine"):
            await web_search_tool.ainvoke({"query": "test query"})

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


class TestWebSearchSystemPrompt:
    @pytest.fixture
    def prompt(self):
        return WEB_SEARCH_SYSTEM_PROMPT.format(current_year=2026, previous_year=2025)

    def test_states_year_rule(self, prompt):
        assert "Year in queries:" in prompt
        assert "Use the current year (2026) in queries" in prompt

    def test_states_sources_rule(self, prompt):
        assert "Sources:" in prompt
        assert (
            "List each result you used as `[<title>](<link>)`. "
            'Skip entries with an empty `link` (the "Suggested answer" hint).'
        ) in prompt

    @pytest.mark.parametrize("emphasis", ["MANDATORY", "CRITICAL REQUIREMENT", "You MUST"])
    def test_has_no_emphasis_markers(self, prompt, emphasis):
        assert emphasis not in prompt
