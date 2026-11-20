import functools
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin

from django.templatetags.static import static
from django.urls import reverse

import yaml

from blog.rendering import render_body, static_path
from daiv import CANONICAL_ORIGIN

if TYPE_CHECKING:
    from collections.abc import Iterable

POSTS_DIR = Path(__file__).parent / "posts"

logger = logging.getLogger("daiv.blog")

_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FRONT_MATTER = re.compile(r"\A---\n(.*?)^---$\n?(.*)\Z", re.DOTALL | re.MULTILINE)
_REQUIRED: dict[str, type] = {"title": str, "date": date, "summary": str, "author": str}
_OPTIONAL: dict[str, type] = {"image": str, "draft": bool, "updated": date}


class PostError(ValueError):
    """A post file that can't be published as written."""


@dataclass(frozen=True)
class Post:
    slug: str
    title: str
    date: date
    summary: str
    author: str
    image: str | None
    draft: bool
    updated: date | None
    html: str
    feed_html: str

    def is_published(self, on: date) -> bool:
        return not self.draft and self.date <= on

    @property
    def canonical_url(self) -> str:
        return _canonical_url(self.slug)

    @property
    def image_url(self) -> str | None:
        return static(static_path(self.slug, self.image)) if self.image else None

    @property
    def absolute_image_url(self) -> str | None:
        return urljoin(CANONICAL_ORIGIN, self.image_url) if self.image_url else None


def _canonical_url(slug: str) -> str:
    return CANONICAL_ORIGIN + reverse("blog:post", args=[slug])


def _read_front_matter(path: Path) -> tuple[dict, str]:
    match = _FRONT_MATTER.match(path.read_text(encoding="utf-8"))
    if match is None:
        raise PostError(f"{path.name}: the file must start with YAML front-matter between two '---' lines")
    try:
        meta = yaml.safe_load(match[1])
    except yaml.YAMLError as exc:
        raise PostError(f"{path.name}: the front-matter is not valid YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise PostError(f"{path.name}: the front-matter must be a mapping of keys to values")
    return meta, match[2]


def _validate(name: str, meta: dict) -> None:
    unknown = sorted(str(key) for key in meta.keys() - _REQUIRED.keys() - _OPTIONAL.keys())
    if unknown:
        raise PostError(f"{name}: unknown front-matter keys: {', '.join(unknown)}")
    missing = sorted(_REQUIRED.keys() - meta.keys())
    if missing:
        raise PostError(f"{name}: missing required front-matter keys: {', '.join(missing)}")
    for key, kind in (_REQUIRED | _OPTIONAL).items():
        if key not in meta:
            continue
        value = meta[key]
        # A YAML timestamp loads as datetime, a date subclass, and must be rejected too.
        if type(value) is not kind:
            raise PostError(f"{name}: {key!r} must be a {kind.__name__}, got {value!r}")
        if kind is str and not value.strip():
            raise PostError(f"{name}: {key!r} must not be empty")


def parse_post(path: Path) -> Post:
    slug = path.stem
    if not _SLUG.match(slug):
        raise PostError(f"{path.name}: the slug {slug!r} must be lowercase letters and digits joined by hyphens")
    meta, body = _read_front_matter(path)
    _validate(path.name, meta)
    try:
        html, feed_html = render_body(body, slug=slug, base_url=_canonical_url(slug))
        if "image" in meta:
            static(static_path(slug, meta["image"]))
    except ValueError as exc:
        raise PostError(f"{path.name}: {exc}") from exc
    return Post(
        slug=slug,
        title=meta["title"],
        date=meta["date"],
        summary=meta["summary"],
        author=meta["author"],
        image=meta.get("image"),
        draft=meta.get("draft", False),
        updated=meta.get("updated"),
        html=html,
        feed_html=feed_html,
    )


def _newest_first(posts: Iterable[Post]) -> tuple[Post, ...]:
    return tuple(sorted(posts, key=lambda post: (-post.date.toordinal(), post.title)))


def load_posts(directory: Path) -> tuple[Post, ...]:
    """Every post in `directory`, failing on the first one that can't be published as written."""
    return _newest_first(parse_post(path) for path in directory.glob("*.md"))


@functools.cache
def all_posts() -> tuple[Post, ...]:
    """The shipped posts, leaving out (and logging) any that can't be published as written."""
    loaded = []
    for path in POSTS_DIR.glob("*.md"):
        try:
            loaded.append(parse_post(path))
        except PostError:
            logger.exception("Skipping the blog post %s", path.name)
    return _newest_first(loaded)


def today() -> date:
    return datetime.now(UTC).date()


def visible_posts(*, include_unpublished: bool = False) -> tuple[Post, ...]:
    if include_unpublished:
        return all_posts()
    on = today()
    return tuple(post for post in all_posts() if post.is_published(on))


def get_visible_post(slug: str, *, include_unpublished: bool = False) -> Post | None:
    return next((post for post in visible_posts(include_unpublished=include_unpublished) if post.slug == slug), None)
