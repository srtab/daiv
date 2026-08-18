from __future__ import annotations

import logging

from django.db import Error as DatabaseError

from notifications.models import Notification

logger = logging.getLogger("daiv.notifications")


def query_unread_count(user) -> int:
    """Count ``user``'s unread notifications.

    The bell badge's single source of truth: the first page render reads it through
    ``unread_notification_count`` below, and every later update comes from the SSE
    endpoint (``accounts.api.views``) recomputing it on a ``core.ui_events`` poke. Raises
    on ``DatabaseError`` so each caller picks its own degradation — a page render shows 0,
    but a live stream must keep the browser's last value rather than push a zero it cannot
    distinguish from a real one.
    """
    return Notification.objects.filter(recipient=user, read_at__isnull=True).count()


def unread_notification_count(request) -> dict[str, int]:
    """Expose the authenticated user's unread notification count to all templates.

    Provides ``unread_count`` so server-rendered templates (e.g. the notification
    bell badge) display the correct value on initial page load.
    """
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    try:
        return {"unread_count": query_unread_count(request.user)}
    except DatabaseError:
        logger.exception("Failed to fetch unread notification count for user %s", request.user.pk)
        return {"unread_count": 0}
