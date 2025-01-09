from __future__ import annotations

import logging
import textwrap
from typing import TYPE_CHECKING

from langchain_community.utilities.duckduckgo_search import DuckDuckGoSearchAPIWrapper
from langchain_community.utilities.tavily_search import TavilySearchAPIWrapper
from langchain_core.tools import BaseTool
from pydantic import BaseModel  # noqa: TC002

from automation.conf import settings

from .schemas import WebSearchInput

if TYPE_CHECKING:
    from langchain.callbacks.manager import CallbackManagerForToolRun

logger = logging.getLogger("daiv.tools")

WEB_SEARCH_NAME = "web_search"


class WebSearchTool(BaseTool):
    """
    Tool for performing web searches to retrieve up-to-date information.
    """

    name: str = WEB_SEARCH_NAME
    description: str = textwrap.dedent(
        """\
        Perform a web search to retrieve current information from the internet.
        Use this tool when you need to access up-to-date information that might not be available in the repository
        or when you need to verify or supplement your knowledge with current data.
        The search results will include relevant snippets from web pages.
        """
    )
    args_schema: type[BaseModel] = WebSearchInput
    handle_validation_error: bool = True

    def _run(self, query: str, intent: str = "", run_manager: CallbackManagerForToolRun | None = None) -> str:
        """
        Performs a web search using DuckDuckGo and returns relevant results.

        Args:
            query: The search query.
            intent: The purpose of the search query.
            run_manager: The callback manager for tool runs.

        Returns:
            A formatted string containing search results.
        """

        logger.debug("[%s] Performing web search for '%s' (intent: %s)", self.name, query, intent)

        if not (results := self._get_results(query)):
            return "No relevant results found for the given search query."

        return "\n".join([
            textwrap.dedent(
                """\
                <SearchResult>
                {body}
                </SearchResult>
                    """
            ).format(body=result)
            for result in results
        ])

    def _get_results(self, query: str) -> list[str]:
        if settings.WEB_SEARCH_ENGINE == "duckduckgo":
            return self._get_duckduckgo_results(query)
        elif settings.WEB_SEARCH_ENGINE == "tavily":
            return self._get_tavily_results(query)
        else:
            raise ValueError(f"Invalid web search engine: {settings.WEB_SEARCH_ENGINE}")

    def _get_duckduckgo_results(self, query: str) -> list[str]:
        api_wrapper = DuckDuckGoSearchAPIWrapper()
        return [result["snippet"] for result in api_wrapper.results(query, max_results=settings.WEB_SEARCH_MAX_RESULTS)]

    def _get_tavily_results(self, query: str) -> list[str]:
        api_wrapper = TavilySearchAPIWrapper()
        return [result["content"] for result in api_wrapper.results(query, max_results=settings.WEB_SEARCH_MAX_RESULTS)]
