from datetime import UTC, datetime, time

from django.contrib.syndication.views import Feed
from django.http import Http404

from blog import posts
from daiv import CANONICAL_ORIGIN


class BlogFeed(Feed):
    title = "DAIV blog"
    link = f"{CANONICAL_ORIGIN}/blog/"
    description = "Open-source, self-hosted SWE agents for GitLab & GitHub"

    def get_object(self, request, *args, **kwargs) -> tuple[posts.Post, ...]:
        visible = posts.visible_posts()
        if not visible:
            raise Http404("No blog posts yet")
        return visible

    def items(self, obj: tuple[posts.Post, ...]) -> tuple[posts.Post, ...]:
        return obj

    def item_title(self, item: posts.Post) -> str:
        return item.title

    def item_description(self, item: posts.Post) -> str:
        return item.feed_html

    def item_link(self, item: posts.Post) -> str:
        return item.canonical_url

    def item_pubdate(self, item: posts.Post) -> datetime:
        return datetime.combine(item.date, time.min, tzinfo=UTC)

    def item_author_name(self, item: posts.Post) -> str:
        return item.author
