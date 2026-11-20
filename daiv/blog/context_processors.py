import logging
from typing import Any

from django.conf import settings
from django.utils.functional import SimpleLazyObject

from blog import posts

logger = logging.getLogger("daiv.blog")


def _has_visible_posts() -> bool:
    try:
        return bool(posts.visible_posts(include_unpublished=settings.DEBUG))
    except Exception:
        logger.exception("The blog posts failed to load, so the Blog link is hidden")
        return False


def blog(request) -> dict[str, Any]:
    """Whether to link the blog, resolved only when a template asks (the landing nav and footer)."""
    return {"blog_has_posts": SimpleLazyObject(_has_visible_posts)}
