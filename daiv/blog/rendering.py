import posixpath
from typing import TYPE_CHECKING, override
from urllib.parse import urljoin, urlsplit

from django.templatetags.static import static

from markdown import Markdown
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

_EXTENSIONS = ["fenced_code", "tables", "toc", "footnotes", "attr_list", "md_in_html"]
_CODEHILITE = {"codehilite": {"css_class": "codehilite", "guess_lang": False}}


def static_path(slug: str, src: str) -> str:
    """The static-files path of `src` inside the post's folder, with `.` and `..` resolved.

    Raises:
        ValueError: when `src` leads outside `blog/<slug>/`.
    """
    folder = f"blog/{slug}/"
    path = posixpath.normpath(folder + src)
    if not path.startswith(folder):
        raise ValueError(f"the image {src!r} points outside the post's folder {folder}")
    return path


class _UrlRewriter(Treeprocessor):
    def __init__(self, md: Markdown, *, slug: str, base_url: str | None):
        super().__init__(md)
        self.slug = slug
        self.base_url = base_url

    @override
    def run(self, root: Element) -> None:
        for img in root.iter("img"):
            src = img.get("src", "")
            if src and not urlsplit(src).scheme and not src.startswith(("/", "#")):
                img.set("src", static(static_path(self.slug, src)))
        if self.base_url is None:
            return
        for tag, attr in (("img", "src"), ("a", "href")):
            for element in root.iter(tag):
                url = element.get(attr, "")
                if url and not url.startswith("#") and not urlsplit(url).scheme:
                    element.set(attr, urljoin(self.base_url, url))


class _UrlExtension(Extension):
    def __init__(self, *, slug: str, base_url: str | None):
        super().__init__()
        self.slug = slug
        self.base_url = base_url

    @override
    def extendMarkdown(self, md: Markdown) -> None:
        md.treeprocessors.register(_UrlRewriter(md, slug=self.slug, base_url=self.base_url), "blog_urls", 1)


def render_body(text: str, *, slug: str, base_url: str) -> tuple[str, str]:
    """Render a post body twice: highlighted for the page, and for the feed with every URL resolved against
    `base_url`, the post's canonical URL.

    Raises:
        ValueError: when an image points outside the post's folder or is missing from the static manifest.
    """
    page = Markdown(
        extensions=[*_EXTENSIONS, "codehilite", _UrlExtension(slug=slug, base_url=None)], extension_configs=_CODEHILITE
    )
    feed = Markdown(extensions=[*_EXTENSIONS, _UrlExtension(slug=slug, base_url=base_url)])
    return page.convert(text), feed.convert(text)
