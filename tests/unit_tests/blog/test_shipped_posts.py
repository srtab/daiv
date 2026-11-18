import re
from pathlib import Path
from urllib.parse import urlsplit

import blog
from blog import posts

BLOG_STATIC = Path(blog.__file__).parent / "static"


def test_every_shipped_post_loads_and_its_images_exist():
    for post in posts.load_posts(posts.POSTS_DIR):
        srcs = re.findall(r'src="/static/(blog/[^"]+)"', post.html)
        if post.image:
            srcs.append(f"blog/{post.slug}/{post.image}")
        for src in srcs:
            assert (BLOG_STATIC / src).is_file(), f"{post.slug}: missing {src}"


def test_every_shipped_post_has_only_absolute_urls_in_the_feed():
    for post in posts.load_posts(posts.POSTS_DIR):
        for url in re.findall(r'(?:src|href|poster)="([^"]*)"', post.feed_html):
            assert url.startswith("#") or urlsplit(url).scheme, f"{post.slug}: relative URL {url!r} in the feed"
