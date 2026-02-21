from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Annotated
from urllib.parse import urljoin, urlparse, urlunparse

from django.core.cache import cache

import markdownify
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from automation.agent.base import BaseAgent
from automation.conf import settings
from daiv import USER_AGENT

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger("daiv.tools")

WEB_FETCH_NAME = "web_fetch"

WEB_FETCH_TOOL_DESCRIPTION = """\
Fetch content from a specified URL and process it using an AI model.

- Takes a URL and a prompt as input
- Fetches the URL content, converts HTML to markdown
- Processes the content with the prompt using a small, fast model
- Returns the model's response about the content
- Caches the full tool response (by URL + prompt) using Django's cache backend

Usage notes:
  - The URL must be a fully-formed valid URL
  - HTTP URLs will be automatically upgraded to HTTPS
  - This tool is read-only and does not modify any files
  - When a URL redirects to a different host, the tool will inform you and provide the redirect URL in a special format:
      <redirect_url>https://new-host.example/path</redirect_url>"""


WEB_FETCH_SYSTEM_PROMPT = f"""\
## Web Fetch tool `{WEB_FETCH_NAME}`

You have access to a `{WEB_FETCH_NAME}` tool to retrieve and analyze web content.

Usage notes:
 - Provide a fully-formed URL.
 - HTTP URLs will be upgraded to HTTPS.
 - If the URL redirects to a different host, the tool will return a redirect tag:
   `<redirect_url>...</redirect_url>` and you should call `{WEB_FETCH_NAME}` again with that URL.
 - This tool is read-only and does not modify any files.
"""


def _upgrade_http_to_https(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme == "http":
        return urlunparse(("https", parsed.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
    return url


def _is_valid_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


async def _fetch_url_text(url: str, *, timeout_seconds: int, proxy_url: str | None) -> tuple[str, str, str]:
    """
    Returns (final_url, content_type, page_raw).
    """
    from httpx import AsyncClient, HTTPError

    async with AsyncClient(proxy=proxy_url, follow_redirects=False) as client:
        try:
            response = await client.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout_seconds)
        except HTTPError as e:
            raise ValueError(f"Failed to fetch {url}: {e!r}") from e

    # Handle redirects manually so we can detect cross-host redirects.
    if 300 <= response.status_code < 400 and response.headers.get("location"):
        redirect_url = urljoin(url, response.headers["location"])
        if urlparse(redirect_url).netloc != urlparse(url).netloc:
            # Special format required by the webfetch tool prompt.
            raise RuntimeError(f"<redirect_url>{redirect_url}</redirect_url>")

        # Same-host redirects are fine to follow automatically (e.g., path normalization).
        return await _fetch_url_text(redirect_url, timeout_seconds=timeout_seconds, proxy_url=proxy_url)

    if response.status_code >= 400:
        raise ValueError(f"Failed to fetch {url} - status code {response.status_code}")

    return (str(response.url), response.headers.get("content-type", ""), response.text)


def _cache_key_for_response(*, url: str, prompt: str) -> str:
    digest = hashlib.sha256(f"{url}\n{prompt}".encode()).hexdigest()
    return f"web_fetch:response:{digest}"


def _get_cached_response(*, url: str, prompt: str) -> str | None:
    return cache.get(_cache_key_for_response(url=url, prompt=prompt))


def _set_cached_response(*, url: str, prompt: str, response: str) -> None:
    cache.set(_cache_key_for_response(url=url, prompt=prompt), response, timeout=settings.WEB_FETCH_CACHE_TTL_SECONDS)


async def _fetch_markdown_for_url(url: str) -> str:
    """
    Fetch the URL and return markdown content.
    """
    final_url, content_type, page_raw = await _fetch_url_text(
        url, timeout_seconds=settings.WEB_FETCH_TIMEOUT_SECONDS, proxy_url=settings.WEB_FETCH_PROXY_URL
    )

    is_html = "<html" in page_raw[:200].lower() or ("text/html" in content_type) or not content_type

    return markdownify.markdownify(page_raw, heading_style=markdownify.ATX) if is_html else page_raw


@tool(WEB_FETCH_NAME, description=WEB_FETCH_TOOL_DESCRIPTION)
async def web_fetch_tool(
    url: Annotated[str, "A fully-formed URL (http/https). HTTP will be upgraded to HTTPS."],
    prompt: Annotated[str, "What you want to extract/analyze from the page."],
) -> str:
    """
    Fetch a URL, convert HTML to markdown, then answer the given prompt using a small/fast model.
    """
    url = _upgrade_http_to_https(url.strip())
    if not _is_valid_http_url(url):
        return "Invalid URL. Provide a fully-formed http(s) URL (e.g., https://example.com)."

    prompt = prompt or ""

    # Cache the final response for a given (url, prompt, model).
    if prompt.strip() and (cached := _get_cached_response(url=url, prompt=prompt)) is not None:
        return str(cached)

    try:
        content = await _fetch_markdown_for_url(url)
    except RuntimeError as e:
        # Used for special redirect signaling.
        return str(e)
    except Exception as e:
        return f"Failed to fetch URL: {e}"

    # Safety guard: avoid silently truncating; ask for a narrower URL/prompt instead.
    if len(content) > settings.WEB_FETCH_MAX_CONTENT_CHARS:
        return (
            "Page content is too large to safely analyze in one pass.\n"
            "Provide a more specific URL (e.g. a specific section/anchor) or narrow the prompt."
        )

    if not prompt.strip() or settings.WEB_FETCH_MODEL_NAME is None:
        return f"Contents of {url}:\n{content}"

    try:
        model = BaseAgent.get_model(model=settings.WEB_FETCH_MODEL_NAME)
        messages = [
            SystemMessage(
                content=(
                    "You process web pages for users. Use the page content to answer the user's prompt.\n"
                    "Be concise. If the content doesn't contain the answer, say so."
                )
            ),
            HumanMessage(content=f"URL: {url}\n\n<PageContent>\n{content}\n</PageContent>\n\nPrompt:\n{prompt}"),
        ]
        response = await model.ainvoke(messages)
        response_text = str(getattr(response, "content", response))
        _set_cached_response(url=url, prompt=prompt, response=response_text)
        return response_text
    except Exception as e:
        logger.warning("web_fetch model processing failed; returning content instead.", exc_info=True)
        return f"Model processing failed ({e}). Contents of {url}:\n{content}"


class WebFetchMiddleware(AgentMiddleware):
    """
    Middleware to add the web_fetch tool to the agent.
    """

    def __init__(self) -> None:
        self.tools = [web_fetch_tool]

    async def awrap_model_call(
        self, request: ModelRequest, handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelResponse:
        system_prompt = ""
        if request.system_prompt:
            system_prompt = request.system_prompt + "\n\n"
        system_prompt += WEB_FETCH_SYSTEM_PROMPT

        return await handler(request.override(system_prompt=system_prompt))
