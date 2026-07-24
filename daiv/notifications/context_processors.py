from __future__ import annotations

import logging

from django.db import Error as DatabaseError

from notifications.choices import EventType
from notifications.models import Notification

logger = logging.getLogger("daiv.notifications")


def unread_notification_count(request) -> dict[str, int]:
    """Expose the authenticated user's unread notification count to all templates.

    Provides ``unread_count`` so server-rendered templates (e.g. the notification
    bell badge) display the correct value on initial page load.
    """
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    try:
        # RUN_FEED rows are the console Feed's per-user slice, not bell events — exclude them
        # so Feed rows never inflate the bell badge (Story 2.3, AC8).
        return {
            "unread_count": Notification.objects
            .filter(recipient=request.user, read_at__isnull=True)
            .exclude(event_type=EventType.RUN_FEED)
            .count()
        }
    except DatabaseError:
        logger.exception("Failed to fetch unread notification count for user %s", request.user.pk)
        return {"unread_count": 0}
