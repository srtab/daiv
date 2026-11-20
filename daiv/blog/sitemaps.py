from typing import TYPE_CHECKING

from django.urls import reverse

from dashboard.sitemaps import CanonicalSitemap

from blog import posts

if TYPE_CHECKING:
    from datetime import date


class BlogIndexSitemap(CanonicalSitemap):
    changefreq = "weekly"

    def items(self) -> list[str]:
        return ["blog:index"] if posts.visible_posts() else []

    def location(self, item: str) -> str:
        return reverse(item)


class BlogPostSitemap(CanonicalSitemap):
    changefreq = "monthly"

    def items(self) -> tuple[posts.Post, ...]:
        return posts.visible_posts()

    def location(self, item: posts.Post) -> str:
        return reverse("blog:post", args=[item.slug])

    def lastmod(self, item: posts.Post) -> date:
        return item.updated or item.date
