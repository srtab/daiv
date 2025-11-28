from __future__ import annotations

import logging
import textwrap
from typing import TYPE_CHECKING, Annotated

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_community.utilities.duckduckgo_search import DuckDuckGoSearchAPIWrapper
from langchain_community.utilities.tavily_search import TavilySearchAPIWrapper
from langchain_core.tools import tool

from automation.conf import settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger("daiv.tools")

WEB_SEARCH_NAME = "web_search"

WEB_SEARCH_TOOL_DESCRIPTION = f"""\
Search the web for programming documentation and solutions.

Usage:
  - Find programming language documentation and tutorials
  - Search for error solutions and debugging help
  - Get latest library versions and installation guides
  - Find code examples and implementation patterns

Examples:
  - Search documentation: `{WEB_SEARCH_NAME}(query="Python requests library documentation")`
  - Find solutions: `{WEB_SEARCH_NAME}(query="TypeError: 'NoneType' object is not callable")`
  - Get latest library versions: `{WEB_SEARCH_NAME}(query="Pandas latest version")`
"""


WEB_SEARCH_SYSTEM_PROMPT = """\
## Web Search tool

You have access to the `{WEB_SEARCH_NAME}` tool to search the web for programming documentation and solutions.

Use this tool to search the web for information that is not available in the repository."""


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


@tool(WEB_SEARCH_NAME, description=WEB_SEARCH_TOOL_DESCRIPTION)
async def web_search_tool(query: Annotated[str, "The search query."]) -> str:
    """
    Tool to search the web and use the results to inform responses.
    """  # noqa: E501

    logger.info("[%s] Performing web search for '%s'", web_search_tool.name, query)

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


class WebSearchMiddleware(AgentMiddleware):
    """
    Middleware to add the web search tool to the agent.
    """

    name = "web_search_middleware"

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
        request = request.override(system_prompt=request.system_prompt + "\n\n" + WEB_SEARCH_SYSTEM_PROMPT)
        return await handler(request)
