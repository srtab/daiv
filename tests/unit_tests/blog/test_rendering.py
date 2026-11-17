from blog.rendering import render_body

ORIGIN = "https://daivagent.com"


def test_code_is_highlighted_on_the_page_and_plain_in_the_feed():
    page, feed = render_body("```python\nx = 1\n```", slug="egress", origin=ORIGIN)
    assert 'class="codehilite"' in page
    assert '<pre><code class="language-python">x = 1' in feed
    assert "codehilite" not in feed


def test_relative_image_resolves_to_the_posts_static_folder():
    page, feed = render_body("![Flow](img/flow.png)", slug="egress", origin=ORIGIN)
    assert 'src="/static/blog/egress/img/flow.png"' in page
    assert 'src="https://daivagent.com/static/blog/egress/img/flow.png"' in feed


def test_root_relative_link_is_absolute_in_the_feed_only():
    page, feed = render_body("[OpenHands](/compare/openhands/)", slug="egress", origin=ORIGIN)
    assert 'href="/compare/openhands/"' in page
    assert 'href="https://daivagent.com/compare/openhands/"' in feed


def test_external_and_fragment_urls_are_untouched():
    md = "![x](https://example.com/a.png) [y](https://example.com/) [z](#why)"
    for html in render_body(md, slug="egress", origin=ORIGIN):
        assert 'src="https://example.com/a.png"' in html
        assert 'href="https://example.com/"' in html
        assert 'href="#why"' in html


def test_raw_html_passes_through():
    md = '<figure><video src="https://example.com/v.mp4"></video></figure>'
    for html in render_body(md, slug="egress", origin=ORIGIN):
        assert md in html


def test_headings_get_anchor_ids():
    page, _ = render_body("## Why it matters", slug="egress", origin=ORIGIN)
    assert 'id="why-it-matters"' in page
