from django.conf import settings
from django.http import Http404

from dashboard.views import LandingPageView

from blog import posts


def _status(post: posts.Post) -> str | None:
    if post.draft:
        return "Draft"
    if not post.is_published(posts.today()):
        return "Scheduled"
    return None


class BlogIndexView(LandingPageView):
    template_name = "blog/index.html"

    def get_context_data(self, **kwargs):
        visible = posts.visible_posts(include_unpublished=settings.DEBUG)
        if not visible:
            raise Http404("No blog posts yet")
        return super().get_context_data(posts=[(post, _status(post)) for post in visible], **kwargs)


class BlogPostView(LandingPageView):
    template_name = "blog/post.html"

    def get_context_data(self, **kwargs):
        post = posts.get_visible_post(kwargs["slug"], include_unpublished=settings.DEBUG)
        if post is None:
            raise Http404("No blog post with that name")
        return super().get_context_data(post=post, status=_status(post), **kwargs)
