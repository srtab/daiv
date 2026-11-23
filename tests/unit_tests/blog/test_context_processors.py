from django.test import Client
from django.urls import reverse

import pytest
from blog import posts
from bs4 import BeautifulSoup

pytestmark = pytest.mark.django_db


def _blog_links(content):
    soup = BeautifulSoup(content, "html.parser")
    places = ("nav.landing-nav", "footer.landing-footer")
    return [bool(soup.select_one(f"{place} a[href='/blog/']")) for place in places]


def test_nav_and_footer_link_the_blog_when_a_post_is_visible(write_post):
    write_post()
    assert _blog_links(Client().get(reverse("homepage")).content) == [True, True]


def test_no_blog_link_without_visible_posts(write_post):
    write_post(draft=True)
    assert _blog_links(Client().get(reverse("homepage")).content) == [False, False]


def test_broken_post_hides_the_link_and_keeps_the_homepage_up(posts_dir, caplog):
    (posts_dir / "broken.md").write_text("no front-matter\n")
    response = Client().get(reverse("homepage"))
    assert response.status_code == 200
    assert _blog_links(response.content) == [False, False]
    assert any(r.name == "daiv.blog" and r.levelname == "ERROR" for r in caplog.records)


def test_app_pages_never_load_posts(member_client, monkeypatch):
    monkeypatch.setattr(posts, "all_posts", lambda: pytest.fail("posts loaded"))
    assert member_client.get(reverse("dashboard")).status_code == 200


def test_unexpected_error_hides_the_link_and_keeps_the_homepage_up(monkeypatch, caplog):
    def explode():
        raise RuntimeError("boom")

    monkeypatch.setattr(posts, "all_posts", explode)
    response = Client().get(reverse("homepage"))
    assert response.status_code == 200
    assert _blog_links(response.content) == [False, False]
    assert any(r.name == "daiv.blog" and r.levelname == "ERROR" for r in caplog.records)
