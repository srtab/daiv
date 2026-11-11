from __future__ import annotations

import asyncio
import copy
import hashlib
import ipaddress
import logging
import re
from typing import TYPE_CHECKING, Annotated, NamedTuple
from urllib.parse import unquote, urldefrag, urljoin, urlparse, urlunparse

from django.core.cache import cache

import markdownify
from bs4 import BeautifulSoup, Tag
from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from automation.agent.base import get_model
from automation.conf import settings as automation_env_settings
from core.site_settings import site_settings
from daiv import USER_AGENT

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Iterator

    from bs4 import PageElement
    from pydantic import SecretStr

logger = logging.getLogger("daiv.tools")

WEB_FETCH_NAME = "web_fetch"

# Tighter than the site-wide model timeout: the page is already size-bounded and a failed
# summary degrades to the raw content.
WEB_FETCH_MODEL_TIMEOUT_SECONDS = 60

_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_LISTED_HEADING_TAGS = ("h1", "h2", "h3")
_MAX_LISTED_ANCHORS = 60
_SELF_LINK = re.compile(r"^#.+")

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
  - A `#fragment` naming a section anchor of an HTML page (e.g. https://docs.example.com/releases/#breaking-changes) \
processes only that section; a page too large to process lists the section anchors it has
  - This tool is read-only and does not modify any files
  - When a URL redirects to a different host, the tool will inform you and provide the redirect URL in a special format:
      <redirect_url>https://new-host.example/path</redirect_url>"""


WEB_FETCH_SYSTEM_PROMPT = f"""\
## Web Fetch tool `{WEB_FETCH_NAME}`

Use the `{WEB_FETCH_NAME}` tool (load it via `tool_search` if it is not loaded) to retrieve and analyze web content.

Usage notes:
 - Provide a fully-formed URL.
 - HTTP URLs will be upgraded to HTTPS.
 - If the URL redirects to a different host, the tool will return a redirect tag:
   `<redirect_url>...</redirect_url>` and you should call `{WEB_FETCH_NAME}` again with that URL.
 - This tool is read-only and does not modify any files.
"""


def _get_auth_headers_for_url(url: str) -> dict[str, str]:
    """
    Return authentication headers configured for the given URL's domain.

    Matching is exact: a configured domain of ``context7.com`` matches
    only ``context7.com`` and not ``api.context7.com`` or ``notcontext7.com``.
    """
    hostname = urlparse(url).hostname or ""
    matched: list[tuple[str, dict[str, SecretStr]]] = []
    for domain, headers in site_settings.web_fetch_auth_headers.items():
        if hostname == domain:
            matched.append((domain, headers))
    # Merge from least-specific to most-specific so longer domains win.
    matched.sort(key=lambda item: len(item[0]))
    result: dict[str, str] = {}
    for _domain, headers in matched:
        result.update({key: value.get_secret_value() for key, value in headers.items()})
    return result


def _upgrade_http_to_https(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme == "http":
        return urlunparse(("https", parsed.netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
    return url


def _is_private_or_local(hostname: str) -> bool:
    """
    Check if a hostname is a private/local IP address or localhost.
    """
    # Check hostname literals first
    if hostname.lower() in {"localhost", "localhost.localdomain"}:
        return True

    try:
        ip = ipaddress.ip_address(hostname)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
    except ValueError:
        # Not a valid IP address, could be a hostname
        # Check for localhost-like patterns
        return hostname.lower().endswith(".local") or hostname.lower().endswith(".localhost")


def _is_valid_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


_MAX_REDIRECTS = 5


class _CrossHostRedirectError(Exception):
    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.url = url


async def _fetch_url_text(
    url: str,
    *,
    timeout_seconds: int,
    proxy_url: str | None,
    extra_headers: dict[str, str] | None = None,
    _redirects_left: int = _MAX_REDIRECTS,
) -> tuple[str, str, str]:
    """
    Returns (final_url, content_type, page_raw).
    """
    from httpx import AsyncClient, HTTPError

    # SSRF protection: block private/local addresses (checked on every redirect to guard against DNS rebinding).
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if _is_private_or_local(hostname):
        raise ValueError(f"Requests to private/local addresses are blocked: {url}")

    request_headers = {"User-Agent": USER_AGENT, **(extra_headers or {})}

    async with AsyncClient(proxy=proxy_url, follow_redirects=False) as client:
        try:
            response = await client.get(url, headers=request_headers, timeout=timeout_seconds)
        except HTTPError as e:
            raise ValueError(f"Failed to fetch {url}: {e!r}") from e

    # Handle redirects manually so we can detect cross-host redirects.
    if 300 <= response.status_code < 400 and response.headers.get("location"):
        redirect_url = urljoin(url, response.headers["location"])
        if urlparse(redirect_url).netloc != urlparse(url).netloc:
            raise _CrossHostRedirectError(redirect_url)

        if _redirects_left <= 0:
            raise ValueError(f"Too many redirects while fetching {url}")

        # Same-host redirects are fine to follow automatically (e.g., path normalization).
        return await _fetch_url_text(
            redirect_url,
            timeout_seconds=timeout_seconds,
            proxy_url=proxy_url,
            extra_headers=extra_headers,
            _redirects_left=_redirects_left - 1,
        )

    if response.status_code >= 400:
        raise ValueError(f"Failed to fetch {url} - status code {response.status_code}")

    return (str(response.url), response.headers.get("content-type", ""), response.text)


def _cache_key_for_response(*, url: str, prompt: str) -> str:
    digest = hashlib.sha256(f"{url}\n{prompt}".encode()).hexdigest()
    return f"web_fetch:response:{digest}"


def _get_cached_response(*, url: str, prompt: str) -> str | None:
    return cache.get(_cache_key_for_response(url=url, prompt=prompt))


def _set_cached_response(*, url: str, prompt: str, response: str) -> None:
    cache.set(
        _cache_key_for_response(url=url, prompt=prompt), response, timeout=site_settings.web_fetch_cache_ttl_seconds
    )


def _cache_key_for_page(url: str) -> str:
    return f"web_fetch:page:{hashlib.sha256(url.encode()).hexdigest()}"


class _Page(NamedTuple):
    content: str
    fragment_note: str
    anchors: list[str]
    """Anchors to fetch instead, listed only when ``content`` is over the size limit."""
    section: str = ""
    """The fragment ``content`` was narrowed to."""


async def _fetch_markdown_for_url(url: str, fragment: str = "") -> _Page:
    """
    Fetch the URL and return its markdown content (only the ``fragment`` section when the HTML has one).
    """
    page_key = _cache_key_for_page(url)
    if (cached := cache.get(page_key)) is not None:
        content_type, page_raw = cached
    else:
        auth_headers = _get_auth_headers_for_url(url)
        _final_url, content_type, page_raw = await _fetch_url_text(
            url,
            timeout_seconds=site_settings.web_fetch_timeout_seconds,
            proxy_url=automation_env_settings.WEB_FETCH_PROXY_URL,
            extra_headers=auth_headers or None,
        )

    is_html = "<html" in page_raw[:200].lower() or ("text/html" in content_type) or not content_type
    if not is_html:
        note = f"#{fragment} was ignored: only HTML pages can be narrowed to a section." if fragment else ""
        return _Page(content=page_raw, fragment_note=note, anchors=[])
    page = await asyncio.to_thread(_html_page, page_raw, fragment, site_settings.web_fetch_max_content_chars)
    if page.anchors:
        # Listed anchors invite one fetch per section, which then reuse this body.
        cache.set(page_key, (content_type, page_raw), timeout=site_settings.web_fetch_cache_ttl_seconds)
    return page


def _html_page(html: str, fragment: str, max_chars: int) -> _Page:
    soup = BeautifulSoup(html, "html.parser")
    nodes = _section_nodes(soup, fragment) if fragment else []
    if nodes and (content := _section_markdown(nodes)):
        anchors = _section_anchors(_subsection_headings(nodes)) if len(content) > max_chars else []
        return _Page(content=content, fragment_note="", anchors=anchors, section=fragment)
    content = _to_markdown(soup)
    return _Page(
        content=content,
        fragment_note=f"The page has no #{fragment} section." if fragment else "",
        anchors=_section_anchors(soup.find_all(_LISTED_HEADING_TAGS)) if len(content) > max_chars else [],
    )


def _to_markdown(node: Tag) -> str:
    return markdownify.MarkdownConverter(heading_style=markdownify.ATX).convert_soup(node)


def _heading_level(heading: Tag) -> int:
    return int(heading.name[1])


def _has_text(node: Tag) -> bool:
    return any(text.strip() for text in node.strings)


def _wrapped_heading(node: Tag | None) -> Tag | None:
    """The heading ``node`` only wraps (GitHub's ``div.markdown-heading``), else ``None``."""
    if node is None or (heading := node.find(_HEADING_TAGS)) is None:
        return None
    return heading if heading.get_text(strip=True) == node.get_text(strip=True) else None


def _section_heading(target: Tag) -> Tag | None:
    """The heading that opens the section ``target`` anchors, or ``None`` when ``target`` is the section itself."""
    if target.name in _HEADING_TAGS:
        return target
    if heading := target.find_parent(_HEADING_TAGS) or _wrapped_heading(target):
        return heading
    if _has_text(target):
        return None
    if heading := _wrapped_heading(target.parent):
        return heading
    following = target.find_next_sibling(_has_text)
    if following is not None and following.name in _HEADING_TAGS:
        return following
    return _wrapped_heading(following)


def _section_target(soup: BeautifulSoup, fragment: str) -> Tag | None:
    fragment = unquote(fragment)
    target = soup.find(id=fragment) or soup.find(None, attrs={"name": fragment})
    if target is not None:
        return target
    links = soup.find_all("a", href=lambda href: href is not None and unquote(href) == f"#{fragment}")
    return next((link for link in links if _section_heading(link)), None)


def _section_nodes(soup: BeautifulSoup, fragment: str) -> list[PageElement]:
    """
    The nodes of the section ``#fragment`` names, in page order; empty when the page has no such section.

    A heading's section runs to the next heading of the same or a higher level; an id on a container
    (Sphinx's ``<section id>``) is the whole container.
    """
    target = _section_target(soup, fragment)
    if target is None:
        return []
    heading = _section_heading(target)
    if heading is None:
        return [target]
    level = _heading_level(heading)
    end = heading.find_next(lambda tag: tag.name in _HEADING_TAGS and _heading_level(tag) <= level)
    return list(_nodes_between(heading, end))


def _nodes_between(start: PageElement, end: Tag | None) -> Iterator[PageElement]:
    """The outermost nodes from ``start`` up to ``end`` (exclusive; ``None`` is the page's end), in page order."""
    holds_end = {id(parent) for parent in end.parents} if end is not None else set()
    node: PageElement | None = start
    while node is not None and node is not end:
        if isinstance(node, Tag) and id(node) in holds_end:
            node = node.contents[0]
            continue
        yield node
        while node is not None and node.next_sibling is None:
            node = node.parent
        node = node.next_sibling if node is not None else None


def _section_markdown(nodes: list[PageElement]) -> str:
    section = BeautifulSoup("", "html.parser")
    for node in nodes:
        section.append(copy.copy(node))
    return _to_markdown(section).strip()


def _subsection_headings(nodes: list[PageElement]) -> list[Tag]:
    """The headings in a section, less the leading one when it outranks the rest (the section's own title)."""
    headings = [
        heading
        for node in nodes
        if isinstance(node, Tag)
        for heading in ([node] if node.name in _HEADING_TAGS else node.find_all(_HEADING_TAGS))
    ]
    if headings and all(_heading_level(heading) > _heading_level(headings[0]) for heading in headings[1:]):
        return headings[1:]
    return headings


def _is_permalink(link: Tag | None) -> bool:
    return link is not None and not any(char.isalnum() for char in link.get_text())


def _heading_anchor(heading: Tag) -> str | None:
    if anchor := heading.get("id"):
        return str(anchor)
    parent = heading.parent
    scope = heading
    # The identity check first keeps this linear: the wrapper test reads the parent's whole text.
    if parent is not None and parent.find(_HEADING_TAGS) is heading and _wrapped_heading(parent) is heading:
        scope = parent
    links = scope.find_all("a", href=_SELF_LINK)
    title = heading.get_text(strip=True)
    link = next((link for link in links if _is_permalink(link)), None) or next(
        (link for link in links if link.get_text(strip=True) == title), None
    )
    return str(link["href"])[1:] if link is not None else None


def _heading_text(heading: Tag) -> str:
    words = [
        text for text in heading.find_all(string=True) if not _is_permalink(text.find_parent("a", href=_SELF_LINK))
    ]
    return " ".join(" ".join(words).split()) or heading.get_text(" ", strip=True)


def _section_anchors(headings: Iterable[Tag]) -> list[str]:
    """``#anchor  Heading`` lines for the ``headings`` that have an anchor, deduplicated in page order."""
    lines = (f"#{anchor}  {_heading_text(heading)}" for heading in headings if (anchor := _heading_anchor(heading)))
    return list(dict.fromkeys(lines))


def _too_large_message(page: _Page) -> str:
    subject = f"The #{page.section} section" if page.section else "Page content"
    lines = [
        f"error: {subject} is too large to analyze in one pass ({len(page.content):,} characters; "
        f"the limit is {site_settings.web_fetch_max_content_chars:,})."
    ]
    if page.fragment_note:
        lines.append(page.fragment_note)
    if page.anchors:
        pick = "one of its subsections" if page.section else "one section"
        lines.append(f"Fetch {pick} instead by adding its anchor to the URL as a #fragment:")
        lines.extend(page.anchors[:_MAX_LISTED_ANCHORS])
        if len(page.anchors) > _MAX_LISTED_ANCHORS:
            lines.append(f"... and {len(page.anchors) - _MAX_LISTED_ANCHORS} more")
    else:
        lines.append("Fetch a narrower URL instead, such as a single release, file or section page.")
    return "\n".join(lines)


@tool(WEB_FETCH_NAME, description=WEB_FETCH_TOOL_DESCRIPTION)
async def web_fetch_tool(
    url: Annotated[str, "A fully-formed URL (http/https). HTTP will be upgraded to HTTPS."],
    prompt: Annotated[str, "What you want to extract/analyze from the page."],
) -> str:
    """
    Fetch a URL, convert HTML to markdown, then answer the given prompt using a small/fast model.
    """
    url = _upgrade_http_to_https(url.strip())
    outcome, result = await _web_fetch(url, prompt or "")
    logger.info("[web_fetch] Tool result (outcome=%s, chars=%d, url=%s)", outcome, len(result), url)
    return result


async def _web_fetch(url: str, prompt: str) -> tuple[str, str]:
    """
    Return ``(outcome, tool result)`` for one fetch; the outcome only labels the log line.
    """
    if not _is_valid_http_url(url):
        return "invalid_url", "error: Invalid URL. Provide a fully-formed http(s) URL (e.g., https://example.com)."

    # Cache key is (url, prompt). The summarisation model is intentionally NOT
    # part of the key today — if the active model is rotated and you want fresh
    # answers, also bump ``_cache_key_for_response``.
    if prompt.strip() and (cached := _get_cached_response(url=url, prompt=prompt)) is not None:
        return "cached", str(cached)

    page_url, fragment = urldefrag(url)
    try:
        page = await _fetch_markdown_for_url(page_url, fragment)
    except _CrossHostRedirectError as e:
        redirect_url = e.url
        if fragment and not urlparse(redirect_url).fragment:
            redirect_url += f"#{fragment}"
        # Special format required by the webfetch tool prompt.
        return "redirect", f"<redirect_url>{redirect_url}</redirect_url>"
    except Exception as e:
        return "fetch_error", f"error: Failed to fetch URL: {e}"

    # Safety guard: avoid silently truncating; point at the page's sections instead.
    if len(page.content) > site_settings.web_fetch_max_content_chars:
        return "too_large", _too_large_message(page)

    content = page.content
    notice = f"Note: {page.fragment_note} This is the whole page.\n" if page.fragment_note else ""
    raw = f"{notice}Contents of {url}:\n{content}"
    if not prompt.strip() or site_settings.web_fetch_model_name is None:
        return "raw", raw

    # A build failure is a permanent misconfiguration (disabled provider, missing key, bad spec), so it
    # logs at ERROR; the agent still gets the raw page, flagged as unsummarised.
    try:
        model = get_model(model=site_settings.web_fetch_model_name, timeout=WEB_FETCH_MODEL_TIMEOUT_SECONDS)
    except Exception as e:
        logger.exception(
            "web_fetch summariser model %r could not be built; check the provider configuration.",
            site_settings.web_fetch_model_name,
        )
        return (
            "summariser_unavailable",
            f"web_fetch summariser unavailable ({e}); returning raw content instead.\n{raw}",
        )

    messages = [
        SystemMessage(
            content=(
                "You process web pages for users. Use the page content to answer the user's prompt.\n"
                "Be concise. If the content doesn't contain the answer, say so."
            )
        ),
        HumanMessage(content=f"URL: {url}\n\n<PageContent>\n{content}\n</PageContent>\n\nPrompt:\n{prompt}"),
    ]
    try:
        response = await model.ainvoke(messages)
        response_text = str(response.text)
        if not response_text.strip():
            logger.warning("web_fetch model returned no answer text; returning content instead.")
            return "summariser_empty", f"Model processing returned no answer. {raw}"
        response_text = notice + response_text
        _set_cached_response(url=url, prompt=prompt, response=response_text)
        return "summarised", response_text
    except Exception as e:
        logger.warning("web_fetch model processing failed; returning content instead.", exc_info=True)
        return "summariser_failed", f"Model processing failed ({e}). {raw}"


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
