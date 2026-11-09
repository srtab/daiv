from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Annotated

from django.core.exceptions import ValidationError
from django.utils import timezone

import httpx
from ddgs.exceptions import DDGSException
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_community.utilities.duckduckgo_search import DuckDuckGoSearchAPIWrapper
from langchain_community.utilities.tavily_search import TavilySearchAPIWrapper
from langchain_core.tools import tool

from core.models import WebSearchEngineChoices
from core.site_settings import site_settings
from core.validators import validate_searxng_url

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger("daiv.tools")

WEB_SEARCH_NAME = "web_search"

# Third-party exception text reaches the model, the checkpoint and the trace verbatim.
_ERROR_MAX_CHARS = 500


class WebSearchConfigurationError(ValueError):
    """Operator misconfiguration of the web search engine (unrecognized engine value, missing API key).

    Raised past the tool's catch-all so a misconfiguration, which no rephrasing fixes, fails the
    run instead of degrading into a tool result nobody reads.
    """


class SearXNGSearchError(RuntimeError):
    """A sanitized SearXNG failure that is safe for the model and diagnostics."""


class SearXNGSearch:
    """Own the configured SearXNG endpoint, transport and result normalization."""

    def __init__(self, *, url: str, timeout_seconds: int, max_results: int):
        if not isinstance(url, str) or not url:
            raise WebSearchConfigurationError(
                "SearXNG search URL is missing. Set DAIV_WEB_SEARCH_SEARXNG_URL or use the config UI."
            )
        try:
            validate_searxng_url(url)
        except ValidationError:
            raise WebSearchConfigurationError(
                "SearXNG search URL must be an HTTP(S) endpoint without credentials, query or fragment."
            ) from None
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 120:
            raise WebSearchConfigurationError("SearXNG search timeout must be an integer between 1 and 120 seconds.")
        if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1:
            raise WebSearchConfigurationError("Web search maximum results must be a positive integer.")
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.max_results = max_results

    @classmethod
    def from_settings(cls) -> SearXNGSearch:
        try:
            url = site_settings.web_search_searxng_url
            timeout = site_settings.web_search_searxng_timeout_seconds
            maximum = site_settings.web_search_max_results
        except ValueError, TypeError:
            # Integer environment coercion includes its raw value in the exception.
            raise WebSearchConfigurationError(
                "Invalid SearXNG configuration. Check its URL, timeout and web search result limit."
            ) from None
        return cls(url=url, timeout_seconds=timeout, max_results=maximum)

    async def search(self, query: str) -> list[dict[str, str]]:
        try:
            # An external search bang can redirect to a result site. Keep requests
            # scoped to the operator's endpoint; never follow that redirect.
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=False) as client:
                response = await client.get(self.url, params={"q": query, "format": "json"})
                response.raise_for_status()
        except httpx.TimeoutException:
            raise SearXNGSearchError(
                "SearXNG request timed out. Check service health or the configured timeout."
            ) from None
        except httpx.HTTPStatusError as exc:
            raise SearXNGSearchError(self._http_error(exc.response.status_code)) from None
        except httpx.RequestError:
            raise SearXNGSearchError(
                "SearXNG request failed. Check network access, proxy configuration and TLS trust."
            ) from None
        except OSError, ValueError, httpx.InvalidURL:
            raise SearXNGSearchError(
                "SearXNG transport configuration failed. Check the endpoint, proxy and CA bundle."
            ) from None
        try:
            payload = response.json()
        except ValueError:
            raise SearXNGSearchError(
                "SearXNG returned invalid JSON. Check JSON output and proxy configuration."
            ) from None
        return self._results(payload)

    def _http_error(self, status: int) -> str:
        if status == 403:
            return "SearXNG refused the request (HTTP 403). Enable JSON output and check access permissions."
        if status == 401:
            return "SearXNG authentication is required (HTTP 401). Check the search endpoint's access configuration."
        if status == 429:
            return "SearXNG rate limit reached (HTTP 429). Check the instance's limiter settings."
        if 300 <= status < 400:
            return "SearXNG redirected the request. Configure the final search endpoint; redirects are not followed."
        return f"SearXNG returned HTTP {status}. Check the search service and its proxy."

    def _results(self, payload: object) -> list[dict[str, str]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise SearXNGSearchError("SearXNG returned an invalid search response. Check the endpoint and JSON output.")
        engine_errors = payload.get("unresponsive_engines", [])
        if not isinstance(engine_errors, list):
            raise SearXNGSearchError("SearXNG returned an invalid search response. Check the endpoint and JSON output.")
        results = []
        invalid_results = False
        for result in payload["results"]:
            normalized = self._result(result)
            if normalized is None:
                invalid_results = True
                continue
            results.append(normalized)
            if len(results) >= self.max_results:
                break
        if not results and engine_errors:
            raise SearXNGSearchError(
                "SearXNG returned no usable results and reported engine failures. Check engine health."
            )
        if not results and invalid_results:
            raise SearXNGSearchError("SearXNG returned no usable result entries. Check the endpoint's response format.")
        if results and (engine_errors or invalid_results):
            logger.error("SearXNG returned partial search results; some engines or result entries failed.")
            results[0]["content"] = (
                "[Partial search results: some engines or result entries failed.] " + results[0]["content"]
            )
        return results

    def _result(self, result: object) -> dict[str, str] | None:
        if not isinstance(result, dict) or not isinstance(result.get("url"), str):
            return None
        try:
            url = httpx.URL(result["url"])
        except httpx.InvalidURL:
            return None
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
            return None
        title, content = result.get("title"), result.get("content")
        if title is not None and not isinstance(title, str) or content is not None and not isinstance(content, str):
            return None
        return {"title": title or str(url), "link": str(url), "content": content or ""}


WEB_SEARCH_TOOL_DESCRIPTION = f"""\
Search the web for up-to-date information and recent data, beyond your knowledge cutoff.

Usage examples:
  - Find programming language documentation and tutorials
  - Search for error solutions and debugging help
  - Get latest library versions and installation guides
  - Find code examples and implementation patterns

Examples:
  - Search documentation: `{WEB_SEARCH_NAME}(query="Python requests library documentation")`
  - Find solutions: `{WEB_SEARCH_NAME}(query="TypeError: 'NoneType' object is not callable")`
  - Get latest library versions: `{WEB_SEARCH_NAME}(query="Pandas latest version")`"""


WEB_SEARCH_SYSTEM_PROMPT = f"""\
## Web Search tool

Use the `{WEB_SEARCH_NAME}` tool (load it via `tool_search` if it is not loaded) to search the web and use the results \
to inform your responses.

Use this tool to:
 - Access up-to-date information for current events and recent data
 - Access information beyond your knowledge cutoff

Result format:
  - The tool returns a JSON array of objects with `title`, `link`, and `content` fields.
  - An empty array (`[]`) means no relevant results were found — broaden the query and retry, or tell the user no results exist.
  - A plain string starting with `error:` instead of JSON means the search backend itself failed — this is a tool failure, not a result. Do not rephrase and retry: continue without web results and say so in your answer.
  - Tavily may prepend a synthesized summary as the first entry with `title="Suggested answer"` and `link=""`. Treat it as a hint, not a citable source.

Year in queries:
  - Use the current year ({{current_year}}) in queries about recent information, documentation or events.
  - Example: If today is {{current_year}}-07-15 and the user asks for "latest React docs", search for "React documentation {{current_year}}", NOT "React documentation {{previous_year}}".

Sources:
  - When your answer comes mainly from search results, end it with a "Sources:" section.
  - List each result you used as `[<title>](<link>)`. Skip entries with an empty `link` (the "Suggested answer" hint).
  - Example format:

    [Your answer here]

    Sources:
    - [Source Title 1](https://example.com/1)
    - [Source Title 2](https://example.com/2)"""  # noqa: E501


async def _get_web_search_results(query: str) -> list[dict[str, str]]:
    """
    Search the web using the configured search backend.

    Args:
        query (str): The search query.

    Returns:
        list[dict[str, str]]: A list of search results.
    """
    if site_settings.web_search_engine == WebSearchEngineChoices.DUCKDUCKGO:
        return _get_duckduckgo_results(query)
    elif site_settings.web_search_engine == WebSearchEngineChoices.TAVILY:
        return await _get_tavily_results(query)
    elif site_settings.web_search_engine == WebSearchEngineChoices.SEARXNG:
        return await SearXNGSearch.from_settings().search(query)
    else:
        raise WebSearchConfigurationError(f"Invalid web search engine: {site_settings.web_search_engine}")


def _get_duckduckgo_results(query: str) -> list[dict[str, str]]:
    """
    Search the web using the DuckDuckGo API.

    Args:
        query (str): The search query.

    Returns:
        list[dict[str, str]]: A list of search results.
    """
    api_wrapper = DuckDuckGoSearchAPIWrapper()
    try:
        results = api_wrapper.results(query, max_results=site_settings.web_search_max_results)
    except DDGSException as exc:
        # ddgs raises rather than returning an empty list when every engine matched nothing; this
        # exact sentinel is only produced when every engine returned cleanly with zero rows.
        if str(exc) == "No results found.":
            return []
        raise
    return [{"title": result["title"], "link": result["link"], "content": result["snippet"]} for result in results]


async def _get_tavily_results(query: str) -> list[dict[str, str]]:
    """
    Search the web using the Tavily API.

    Args:
        query (str): The search query.

    Returns:
        list[dict[str, str]]: A list of search results.
    """
    api_key = site_settings.web_search_api_key
    if not api_key:
        raise WebSearchConfigurationError(
            "Web search API key is not configured. Set DAIV_WEB_SEARCH_API_KEY or use the config UI."
        )

    api_wrapper = TavilySearchAPIWrapper(tavily_api_key=api_key.get_secret_value())

    results = await api_wrapper.raw_results_async(
        query, max_results=site_settings.web_search_max_results, include_answer=True
    )
    results_content = [
        {"title": result["title"], "link": result["url"], "content": result["content"]} for result in results["results"]
    ]
    if results["answer"]:
        return [{"title": "Suggested answer", "link": "", "content": results["answer"]}] + results_content
    return results_content


def _truncate(detail: str) -> str:
    if len(detail) > _ERROR_MAX_CHARS:
        return detail[:_ERROR_MAX_CHARS].rstrip() + "…[truncated]"
    return detail


@tool(WEB_SEARCH_NAME, description=WEB_SEARCH_TOOL_DESCRIPTION)
async def web_search_tool(query: Annotated[str, "The search query."]) -> str:
    """
    Tool to search the web and use the results to inform responses.
    """  # noqa: E501

    try:
        results = await _get_web_search_results(query)
    except WebSearchConfigurationError:
        raise
    except SearXNGSearchError as exc:
        # The backend has already replaced upstream exception text and bodies.
        # Do not log a traceback containing the original request or response.
        logger.error("SearXNG web search failed: %s", exc)
        return f"error: Web search failed. Details: {exc}"
    except Exception as exc:
        # Returned, not raised: the ToolNode's default handler re-raises anything that is not a
        # ToolInvocationError, which aborts the whole run and discards every completed step.
        logger.exception("Web search failed for query %r", query)
        return f"error: Web search failed. Details: {_truncate(f'{type(exc).__name__}: {exc}')}"

    # `ensure_ascii=False` keeps non-ASCII titles/snippets readable for the model
    # (and saves tokens vs. \uXXXX escapes). An empty array is a real, valid
    # outcome the model is told how to handle in the system prompt.
    return json.dumps(results, ensure_ascii=False)


class WebSearchMiddleware(AgentMiddleware):
    """
    Middleware to add the web search tool to the agent.
    """

    def __init__(self) -> None:
        """
        Initialize the middleware.
        """
        self.tools = [web_search_tool]

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        """
        Update the system prompt with the web search system prompt.
        """
        current_year = timezone.now().year
        web_search_prompt = WEB_SEARCH_SYSTEM_PROMPT.format(current_year=current_year, previous_year=current_year - 1)
        request = request.override(system_prompt=request.system_prompt + "\n\n" + web_search_prompt)
        return await handler(request)
