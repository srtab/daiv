from __future__ import annotations

import logging
import textwrap
from typing import TYPE_CHECKING

from duckduckgo_search import DDGS
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

        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=settings.WEB_SEARCH_MAX_RESULTS))

        if not results:
            return "No relevant results found for the given query."

        formatted_results = []
        for result in results:
            formatted_results.append(
                textwrap.dedent(
                    """\
                    <SearchResult>
                    Title: {title}
                    Link: {link}
                    Snippet: {body}
                    </SearchResult>
                    """
                ).format(
                    title=result.get("title", "No title"),
                    link=result.get("link", "No link"),
                    body=result.get("body", "No content"),
                )
            )

        return "\n".join(formatted_results)
