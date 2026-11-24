from datetime import date, datetime

import pytest
from blog import posts


def test_parses_front_matter_and_renders_the_body(write_post):
    write_post()
    (post,) = posts.all_posts()
    assert (post.slug, post.title, post.date, post.summary, post.author) == (
        "egress",
        "Egress",
        date(2026, 12, 14),
        "How egress works.",
        "Sandro Rodrigues",
    )
    assert post.image is None and post.draft is False and post.updated is None
    assert "<p>Hello.</p>" in post.html


def test_posts_are_newest_first(write_post):
    write_post(slug="old", date=date(2026, 12, 1))
    write_post(slug="new", date=date(2026, 12, 10))
    assert [p.slug for p in posts.all_posts()] == ["new", "old"]


def test_missing_required_field_names_the_file_and_field(posts_dir):
    (posts_dir / "egress.md").write_text("---\ntitle: Egress\ndate: 2026-12-14\nauthor: S\n---\nHi\n")
    with pytest.raises(posts.PostError, match=r"egress\.md.*summary"):
        posts.load_posts(posts.POSTS_DIR)


def test_unknown_front_matter_key_is_rejected(write_post):
    write_post(drat=True)
    with pytest.raises(posts.PostError, match="drat"):
        posts.load_posts(posts.POSTS_DIR)


def test_file_without_front_matter_is_rejected(posts_dir):
    (posts_dir / "egress.md").write_text("Just text\n")
    with pytest.raises(posts.PostError, match=r"egress\.md"):
        posts.load_posts(posts.POSTS_DIR)


def test_date_must_be_a_date(write_post):
    write_post(date="soon")
    with pytest.raises(posts.PostError, match="date"):
        posts.load_posts(posts.POSTS_DIR)


def test_slug_must_be_url_safe(write_post):
    write_post(slug="Egress_Post")
    with pytest.raises(posts.PostError, match="Egress_Post"):
        posts.load_posts(posts.POSTS_DIR)


def test_post_dated_today_is_visible_and_tomorrow_is_not(write_post):
    write_post(slug="today", date=date(2026, 12, 15))
    write_post(slug="tomorrow", date=date(2026, 12, 16))
    assert [p.slug for p in posts.visible_posts()] == ["today"]
    assert posts.get_visible_post("tomorrow") is None


def test_drafts_are_hidden_unless_asked_for(write_post):
    write_post(draft=True)
    assert posts.visible_posts() == ()
    assert [p.slug for p in posts.visible_posts(include_unpublished=True)] == ["egress"]


def test_urls_point_at_daivagent_com(write_post):
    write_post(image="cover.png")
    (post,) = posts.all_posts()
    assert post.canonical_url == "https://daivagent.com/blog/egress/"
    assert post.image_url == "/static/blog/egress/cover.png"
    assert post.absolute_image_url == "https://daivagent.com/static/blog/egress/cover.png"


def test_date_with_a_time_is_rejected(write_post):
    write_post(date=datetime(2026, 12, 14, 9, 30))
    with pytest.raises(posts.PostError, match="date"):
        posts.load_posts(posts.POSTS_DIR)


def test_a_broken_post_is_skipped_and_logged(write_post, posts_dir, caplog):
    write_post()
    (posts_dir / "broken.md").write_text("no front-matter\n")
    assert [p.slug for p in posts.all_posts()] == ["egress"]
    errors = [r.getMessage() for r in caplog.records if r.name == "daiv.blog" and r.levelname == "ERROR"]
    assert any("broken.md" in message for message in errors)


def test_image_missing_from_the_manifest_names_the_post(write_post, manifest_storage):
    write_post(body="![Gone](img/gone.png)")
    with pytest.raises(posts.PostError, match=r"egress\.md.*gone\.png"):
        posts.load_posts(posts.POSTS_DIR)


def test_cover_image_missing_from_the_manifest_names_the_post(write_post, manifest_storage):
    write_post(image="cover.png")
    with pytest.raises(posts.PostError, match=r"egress\.md.*cover\.png"):
        posts.load_posts(posts.POSTS_DIR)


def test_cover_image_outside_the_post_folder_is_rejected(write_post):
    write_post(image="../other/cover.png")
    with pytest.raises(posts.PostError, match=r"egress\.md.*outside"):
        posts.load_posts(posts.POSTS_DIR)
