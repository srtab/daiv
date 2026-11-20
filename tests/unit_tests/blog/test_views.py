import json
import re
from datetime import date

from django.test import Client

import pytest
from bs4 import BeautifulSoup
from dashboard import landing

pytestmark = pytest.mark.django_db


def test_index_lists_visible_posts_newest_first(write_post):
    write_post(slug="old", title="Old", date=date(2026, 12, 1))
    write_post(slug="new", title="New", date=date(2026, 12, 10))
    write_post(slug="later", date=date(2026, 12, 20))
    soup = BeautifulSoup(Client().get("/blog/").content, "html.parser")
    assert [a["href"] for a in soup.select("main a[href^='/blog/']")] == ["/blog/new/", "/blog/old/"]


def test_index_is_not_found_without_visible_posts(write_post):
    write_post(draft=True)
    assert Client().get("/blog/").status_code == 404


def test_post_page_shows_the_post_and_the_ctas(write_post):
    write_post(body="Body text.")
    content = Client().get("/blog/egress/").content.decode()
    assert "Egress</h1>" in content and "<p>Body text.</p>" in content
    assert "/static/blog/css/highlight.css" in content
    assert f'href="{landing.QUICKSTART_URL}"' in content
    assert 'href="https://github.com/srtab/daiv"' in content


def test_future_draft_and_unknown_posts_are_not_found(write_post):
    write_post(slug="tomorrow", date=date(2026, 12, 16))
    write_post(slug="draft", draft=True)
    for slug in ("tomorrow", "draft", "nothing"):
        assert Client().get(f"/blog/{slug}/").status_code == 404


def test_debug_previews_unpublished_posts_with_a_marker(write_post, settings):
    settings.DEBUG = True
    write_post(slug="tomorrow", date=date(2026, 12, 16))
    write_post(slug="draft", draft=True)
    assert "Scheduled" in Client().get("/blog/tomorrow/").content.decode()
    assert "Draft" in Client().get("/blog/draft/").content.decode()
    assert Client().get("/blog/").status_code == 200


def test_canonical_points_at_daivagent_com_on_any_host(write_post):
    write_post()
    for path in ("/blog/", "/blog/egress/"):
        content = Client(HTTP_HOST="daiv.internal.example").get(path).content.decode()
        assert content.count('rel="canonical"') == 1
        assert f'<link rel="canonical" href="https://daivagent.com{path}">' in content


def test_share_tags_without_an_image(write_post):
    write_post()
    content = Client().get("/blog/egress/").content.decode()
    for tag in (
        '<meta property="og:type" content="article">',
        '<meta property="og:url" content="https://daivagent.com/blog/egress/">',
        '<meta property="og:title" content="Egress">',
        '<meta property="og:description" content="How egress works.">',
        '<meta name="description" content="How egress works.">',
        '<meta property="article:published_time" content="2026-12-14">',
        '<meta name="twitter:card" content="summary">',
    ):
        assert tag in content
    assert "og:image" not in content


def test_share_tags_with_an_image(write_post):
    write_post(image="cover.png")
    content = Client().get("/blog/egress/").content.decode()
    assert '<meta property="og:image" content="https://daivagent.com/static/blog/egress/cover.png">' in content
    assert '<meta name="twitter:card" content="summary_large_image">' in content


def test_json_ld_describes_the_post(write_post):
    write_post()
    content = Client().get("/blog/egress/").content.decode()
    data = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', content, re.DOTALL)[1])
    assert data["@type"] == "BlogPosting"
    assert (data["headline"], data["datePublished"], data["url"], data["author"]["name"]) == (
        "Egress",
        "2026-12-14",
        "https://daivagent.com/blog/egress/",
        "Sandro Rodrigues",
    )
    assert "dateModified" not in data


def test_json_ld_carries_the_update_date(write_post):
    write_post(updated=date(2026, 12, 15))
    content = Client().get("/blog/egress/").content.decode()
    data = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', content, re.DOTALL)[1])
    assert data["dateModified"] == "2026-12-15"


def test_signed_in_user_can_read_a_post(write_post, member_client):
    write_post()
    assert member_client.get("/blog/egress/").status_code == 200
