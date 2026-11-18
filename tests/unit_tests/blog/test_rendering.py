import pytest
from blog.rendering import render_body, static_path

BASE = "https://daivagent.com/blog/egress/"


def test_code_is_highlighted_on_the_page_and_plain_in_the_feed():
    page, feed = render_body("```python\nx = 1\n```", slug="egress", base_url=BASE)
    assert 'class="codehilite"' in page
    assert '<pre><code class="language-python">x = 1' in feed
    assert "codehilite" not in feed


def test_relative_image_resolves_to_the_posts_static_folder():
    page, feed = render_body("![Flow](img/flow.png)", slug="egress", base_url=BASE)
    assert 'src="/static/blog/egress/img/flow.png"' in page
    assert 'src="https://daivagent.com/static/blog/egress/img/flow.png"' in feed


def test_root_relative_link_is_absolute_in_the_feed_only():
    page, feed = render_body("[OpenHands](/compare/openhands/)", slug="egress", base_url=BASE)
    assert 'href="/compare/openhands/"' in page
    assert 'href="https://daivagent.com/compare/openhands/"' in feed


def test_external_and_fragment_urls_are_untouched():
    md = "![x](https://example.com/a.png) [y](https://example.com/) [z](#why)"
    for html in render_body(md, slug="egress", base_url=BASE):
        assert 'src="https://example.com/a.png"' in html
        assert 'href="https://example.com/"' in html
        assert 'href="#why"' in html


def test_raw_html_passes_through():
    md = '<figure><video src="https://example.com/v.mp4"></video></figure>'
    for html in render_body(md, slug="egress", base_url=BASE):
        assert md in html


def test_headings_get_anchor_ids():
    page, _ = render_body("## Why it matters", slug="egress", base_url=BASE)
    assert 'id="why-it-matters"' in page


def test_relative_links_resolve_against_the_post_in_the_feed():
    _, feed = render_body("[csv](results.csv) [next](../agent-memory/)", slug="egress", base_url=BASE)
    assert 'href="https://daivagent.com/blog/egress/results.csv"' in feed
    assert 'href="https://daivagent.com/blog/agent-memory/"' in feed


def test_figure_with_markdown_inside_gets_an_absolute_image_in_the_feed():
    md = '<figure markdown="1">\n![Flow](img/flow.png)\n<figcaption>Flow</figcaption>\n</figure>'
    page, feed = render_body(md, slug="egress", base_url=BASE)
    assert 'src="/static/blog/egress/img/flow.png"' in page
    assert 'src="https://daivagent.com/static/blog/egress/img/flow.png"' in feed


def test_static_path_normalises_dot_segments():
    assert static_path("egress", "./img/../img/flow.png") == "blog/egress/img/flow.png"


def test_dot_segments_resolve_in_a_strict_manifest(manifest_storage):
    page, _ = render_body("![Flow](./img/flow.png)", slug="egress", base_url=BASE)
    assert 'src="/static/blog/egress/img/flow.abc123.png"' in page


def test_image_outside_the_post_folder_is_rejected():
    with pytest.raises(ValueError, match="outside"):
        render_body("![x](../other/flow.png)", slug="egress", base_url=BASE)
