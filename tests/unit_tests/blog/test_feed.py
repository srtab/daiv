import xml.etree.ElementTree as ET  # noqa: S405
from datetime import date

from django.test import Client
from django.urls import reverse

import pytest

pytestmark = pytest.mark.django_db


def _get_feed() -> ET.Element:
    return ET.fromstring(Client().get("/blog/feed/").content)  # noqa: S314


def test_feed_carries_full_bodies_with_absolute_urls(write_post):
    write_post(body="![Flow](img/flow.png) [OpenHands](/compare/openhands/)")
    root = _get_feed()
    assert root.findtext("channel/link") == "https://daivagent.com/blog/"
    (item,) = root.findall("channel/item")
    assert item.findtext("link") == "https://daivagent.com/blog/egress/"
    body = item.findtext("description")
    assert 'src="https://daivagent.com/static/blog/egress/img/flow.png"' in body
    assert 'href="https://daivagent.com/compare/openhands/"' in body
    assert item.findtext("pubDate").startswith("Mon, 14 Dec 2026")


def test_feed_leaves_out_hidden_posts_even_in_debug(write_post, settings):
    settings.DEBUG = True
    write_post(slug="live")
    write_post(slug="draft", draft=True)
    write_post(slug="tomorrow", date=date(2026, 12, 16))
    links = [item.findtext("link") for item in _get_feed().iter("item")]
    assert links == ["https://daivagent.com/blog/live/"]


def test_feed_is_not_found_without_visible_posts(posts_dir):
    assert Client().get("/blog/feed/").status_code == 404


def test_blog_pages_advertise_the_feed(write_post):
    write_post()
    for url in ("/blog/", "/blog/egress/"):
        assert 'type="application/rss+xml" title="DAIV blog" href="/blog/feed/"' in Client().get(url).content.decode()


def test_sitemap_lists_the_index_and_published_posts(write_post):
    write_post()
    write_post(slug="draft", draft=True)
    content = Client().get(reverse("sitemap")).content.decode()
    assert "/blog/</loc>" in content and "/blog/egress/</loc>" in content
    assert "<lastmod>2026-12-14</lastmod>" in content
    assert "/blog/draft/" not in content


def test_sitemap_lastmod_is_the_update_date_when_a_post_has_one(write_post):
    write_post(updated=date(2026, 12, 15))
    content = Client().get(reverse("sitemap")).content.decode()
    assert "<lastmod>2026-12-15</lastmod>" in content
    assert "<lastmod>2026-12-14</lastmod>" not in content


def test_sitemap_has_no_blog_without_visible_posts(posts_dir):
    assert "/blog/" not in Client().get(reverse("sitemap")).content.decode()


def test_a_broken_post_leaves_the_sitemap_and_the_blog_up(write_post, posts_dir):
    write_post()
    (posts_dir / "broken.md").write_text("no front-matter\n")
    assert Client().get(reverse("sitemap")).status_code == 200
    assert Client().get("/blog/").status_code == 200
