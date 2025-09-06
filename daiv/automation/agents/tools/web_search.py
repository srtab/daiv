from __future__ import annotations

import logging
import textwrap

from langchain_community.utilities.duckduckgo_search import DuckDuckGoSearchAPIWrapper
from langchain_community.utilities.tavily_search import TavilySearchAPIWrapper
from langchain_core.tools import tool

from automation.conf import settings

logger = logging.getLogger("daiv.tools")

WEB_SEARCH_NAME = "web_search"


async def _get_web_search_results(query: str) -> list[dict[str, str]]:
    """
    Search the web using the DuckDuckGo or Tavily API.

    Args:
        query (str): The search query.

    Returns:
        list[dict[str, str]]: A list of search results.
    """
    if settings.WEB_SEARCH_ENGINE == "duckduckgo":
        return _get_duckduckgo_results(query)
    elif settings.WEB_SEARCH_ENGINE == "tavily":
        return await _get_tavily_results(query)
    else:
        raise ValueError(f"Invalid web search engine: {settings.WEB_SEARCH_ENGINE}")


def _get_duckduckgo_results(query: str) -> list[dict[str, str]]:
    """
    Search the web using the DuckDuckGo API.

    Args:
        query (str): The search query.

    Returns:
        list[dict[str, str]]: A list of search results.
    """
    api_wrapper = DuckDuckGoSearchAPIWrapper()
    return [
        {"title": result["title"], "link": result["link"], "content": result["snippet"]}
        for result in api_wrapper.results(query, max_results=settings.WEB_SEARCH_MAX_RESULTS)
    ]


async def _get_tavily_results(query: str) -> list[dict[str, str]]:
    """
    Search the web using the Tavily API.

    Args:
        query (str): The search query.

    Returns:
        list[dict[str, str]]: A list of search results.
    """
    assert settings.WEB_SEARCH_API_KEY is not None, "AUTOMATION_WEB_SEARCH_API_KEY is not set"

    api_wrapper = TavilySearchAPIWrapper(tavily_api_key=settings.WEB_SEARCH_API_KEY)

    results = await api_wrapper.raw_results_async(
        query, max_results=settings.WEB_SEARCH_MAX_RESULTS, include_answer=True
    )
    results_content = [
        {"title": result["title"], "link": result["url"], "content": result["content"]} for result in results["results"]
    ]
    if results["answer"]:
        return [{"title": "Suggested answer", "link": "", "content": results["answer"]}] + results_content
    return results_content


@tool(WEB_SEARCH_NAME, parse_docstring=True)
async def web_search_tool(query: str) -> str:
    """
    Search the web and use the results to inform responses.

    - Provides up-to-date information for current events and recent data
    - Returns search result information formatted as search result blocks from the most relevant to the least relevant
    - Use this tool for accessing information beyond the knowledge cutoff
    - Searches are performed automatically within a single API call

    Args:
        query (str): The search query.

    Returns:
        str: A formatted string containing search results.
    """

    logger.debug("[%s] Performing web search for '%s'", web_search_tool.name, query)

    if not (results := await _get_web_search_results(query)):
        return "No relevant results found for the given search query."

    return "\n".join([
        textwrap.dedent(
            """\
            <web_search_result title="{title}" link="{link}">
            {body}
            </web_search_result>
            """
        ).format(title=result["title"], link=result["link"], body=result["content"])
        for result in results
    ])
