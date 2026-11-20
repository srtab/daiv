from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from django.contrib.sitemaps import Sitemap
from django.urls import reverse

from daiv import CANONICAL_ORIGIN
from dashboard.comparisons import CHECKED, COMPARISONS

if TYPE_CHECKING:
    from dashboard.comparisons import Comparison

_CANONICAL = urlsplit(CANONICAL_ORIGIN)


class CanonicalSitemap(Sitemap):
    """A sitemap of the URLs the public pages' canonicals name, whichever install's host serves it."""

    protocol = _CANONICAL.scheme

    def get_domain(self, site=None) -> str:
        return _CANONICAL.netloc


class StaticSitemap(CanonicalSitemap):
    changefreq = "weekly"

    def items(self) -> list[str]:
        return ["homepage"]

    def location(self, item: str) -> str:
        return reverse(item)


class ComparisonSitemap(CanonicalSitemap):
    changefreq = "monthly"
    lastmod = CHECKED

    def items(self) -> tuple[Comparison, ...]:
        return COMPARISONS

    def location(self, item: Comparison) -> str:
        return reverse("comparison", args=[item.slug])
