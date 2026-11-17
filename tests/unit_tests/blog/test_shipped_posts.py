import re
from pathlib import Path

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
