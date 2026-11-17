from typing import TYPE_CHECKING, override
from urllib.parse import urlsplit

from django.templatetags.static import static

from markdown import Markdown
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor

if TYPE_CHECKING:
    from xml.etree.ElementTree import Element

_EXTENSIONS = ["fenced_code", "tables", "toc", "footnotes", "attr_list"]
_CODEHILITE = {"codehilite": {"css_class": "codehilite", "guess_lang": False}}


class _UrlRewriter(Treeprocessor):
    def __init__(self, md: Markdown, *, slug: str, origin: str | None):
        super().__init__(md)
        self.slug = slug
        self.origin = origin

    @override
    def run(self, root: Element) -> None:
        for img in root.iter("img"):
            src = img.get("src", "")
            if src and not urlsplit(src).scheme and not src.startswith(("/", "#")):
                img.set("src", static(f"blog/{self.slug}/{src}"))
        if self.origin is None:
            return
        for tag, attr in (("img", "src"), ("a", "href")):
            for element in root.iter(tag):
                url = element.get(attr, "")
                if url.startswith("/") and not url.startswith("//"):
                    element.set(attr, self.origin + url)


class _UrlExtension(Extension):
    def __init__(self, *, slug: str, origin: str | None):
        super().__init__()
        self.slug = slug
        self.origin = origin

    @override
    def extendMarkdown(self, md: Markdown) -> None:
        md.treeprocessors.register(_UrlRewriter(md, slug=self.slug, origin=self.origin), "blog_urls", 1)


def render_body(text: str, *, slug: str, origin: str) -> tuple[str, str]:
    """Render a post body twice: highlighted for the page, and with absolute URLs for the feed."""
    page = Markdown(
        extensions=[*_EXTENSIONS, "codehilite", _UrlExtension(slug=slug, origin=None)], extension_configs=_CODEHILITE
    )
    feed = Markdown(extensions=[*_EXTENSIONS, _UrlExtension(slug=slug, origin=origin)])
    return page.convert(text), feed.convert(text)
