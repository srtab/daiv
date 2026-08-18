from __future__ import annotations

import logging

from django.db import Error as DatabaseError

from notifications.models import Notification

logger = logging.getLogger("daiv.notifications")


def query_unread_count(user) -> int:
    """Count ``user``'s unread notifications.

    The bell badge's single source of truth: the first page render reads it through
    ``unread_notification_count`` below, and every later update comes from the SSE
    endpoint (``accounts.api.views``) recomputing it on a ``core.ui_events`` poke. Falls
    back to 0 and logs on ``DatabaseError`` so a transient DB failure degrades the badge
    rather than breaking the page (or dropping the stream).
    """
    try:
        return Notification.objects.filter(recipient=user, read_at__isnull=True).count()
    except DatabaseError:
        logger.exception("Failed to fetch unread notification count for user %s", user.pk)
        return 0


def unread_notification_count(request) -> dict[str, int]:
    """Expose the authenticated user's unread notification count to all templates.

    Provides ``unread_count`` so server-rendered templates (e.g. the notification
    bell badge) display the correct value on initial page load.
    """
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    return {"unread_count": query_unread_count(request.user)}
