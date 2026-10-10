"""XSS guard for tool-body rendering.

``toolBodyHTML`` is wired straight into ``x-html`` (raw innerHTML) at
``session_detail.html``, so unlike the markdown path it bypasses nothing — the
HTML it returns IS the DOM. Two sinks carry attacker-controlled URLs:

* ``web_fetch`` — the request URL and any ``<redirect_url>`` payload, via ``externalLink``.
* ``web_search`` — result links, via the same ``externalLink``.

These drive the real ``tool-renderers.js`` under node (the failure they guard is a
string-escape gap a source grep cannot see) and assert the rendered HTML carries
no event-handler attributes and no non-http(s) anchor — i.e. the DOM is inert.
"""

from __future__ import annotations

import json
import re

from tests.unit_tests.chat.tool_renderers_driver import run_tool_renderers
from tests.unit_tests.jsdriver import requires_node

pytestmark = requires_node

_TAG_RE = re.compile(r"<([a-zA-Z][a-zA-Z0-9]*)\b([^>]*)>")
_ATTR_RE = re.compile(r'([a-zA-Z][a-zA-Z0-9:-]*)\s*=\s*"([^"]*)"')
_UNSAFE_HREF_RE = re.compile(r"^(?:javascript|data|vbscript):", re.IGNORECASE)


def _scan_tags(html: str) -> list[dict]:
    """Return ``[{name, attrs: {name: value}}]`` for every tag in ``html``."""
    tags: list[dict] = []
    for m in _TAG_RE.finditer(html):
        attrs = {am.group(1).lower(): am.group(2) for am in _ATTR_RE.finditer(m.group(2))}
        tags.append({"name": m.group(1).lower(), "attrs": attrs})
    return tags


def _assert_inert(html: str) -> None:
    """The rendered HTML must reach the DOM carrying no script sink."""
    assert not re.search(r"<script\b", html, re.IGNORECASE), f"<script> survived: {html!r}"
    for tag in _scan_tags(html):
        for attr_name in tag["attrs"]:
            assert not attr_name.startswith("on"), f"event-handler attr {attr_name!r} in: {html!r}"
        href = tag["attrs"].get("href", "")
        assert not _UNSAFE_HREF_RE.match(href), f"unsafe href {href!r} in: {html!r}"


def test_double_quote_breakout_in_url_is_inert():
    """A URL carrying a literal double-quote tries to close the ``href`` attribute
    and inject an ``onmouseover`` handler. ``escapeHtml`` must escape the quote so
    the whole payload stays inside the href value (the breakout would otherwise
    parse ``onmouseover`` as its own attribute).
    """
    url = 'https://evil.example/" onmouseover="alert(1)"'
    out = run_tool_renderers("web_fetch", json.dumps({"url": url}), "")
    _assert_inert(out["html"])
    anchors = [t for t in _scan_tags(out["html"]) if t["name"] == "a"]
    assert anchors, "an https URL should render as an anchor"
    href = anchors[0]["attrs"]["href"]
    assert "&quot;" in href, f"quote not escaped in href: {href!r}"
    assert "onmouseover" not in anchors[0]["attrs"]
    assert out["dompurifyCalled"] is True


def test_javascript_scheme_url_is_rendered_as_plain_text():
    """A ``javascript:`` URL must never become an anchor; ``externalLink`` emits a
    plain span for non-http(s) schemes so there is no clickable script sink.
    """
    url = "javascript:alert(document.cookie)"
    out = run_tool_renderers("web_fetch", json.dumps({"url": url}), "")
    _assert_inert(out["html"])
    anchors = [t for t in _scan_tags(out["html"]) if t["name"] == "a"]
    assert not anchors, f"javascript: URL rendered as an anchor: {out['html']!r}"


def test_web_search_javascript_link_is_inert():
    """``webSearchBody`` feeds result links into the same ``externalLink`` sink."""
    result = json.dumps([{"title": "click", "link": "javascript:alert(1)", "content": "c"}])
    out = run_tool_renderers("web_search", json.dumps({"query": "q"}), result)
    _assert_inert(out["html"])
    anchors = [t for t in _scan_tags(out["html"]) if t["name"] == "a"]
    assert not anchors, f"javascript: result link rendered as an anchor: {out['html']!r}"


def test_redirect_url_payload_cannot_break_out():
    """The ``<redirect_url>`` payload the model re-calls is extracted and handed to
    ``externalLink``; a payload carrying a script scheme and a quote breakout must
    render inert rather than become a clickable ``javascript:`` anchor.
    """
    payload = 'javascript:alert(1)" onclick="alert(2)'
    out = run_tool_renderers("web_fetch", json.dumps({"url": "https://x"}), f"<redirect_url>{payload}</redirect_url>")
    _assert_inert(out["html"])
    anchors = [t for t in _scan_tags(out["html"]) if t["name"] == "a"]
    assert not anchors, f"javascript: redirect rendered as an anchor: {out['html']!r}"


def test_normal_http_url_still_links():
    """Regression guard: the scheme gate must not regress legitimate http(s) URLs
    into plain text.
    """
    out = run_tool_renderers("web_fetch", json.dumps({"url": "https://example.com/page"}), "")
    anchors = [t for t in _scan_tags(out["html"]) if t["name"] == "a"]
    assert anchors and anchors[0]["attrs"]["href"] == "https://example.com/page"
