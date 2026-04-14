from __future__ import annotations

from notifications.models import Notification


def unread_notification_count(request) -> dict[str, int]:
    """Expose the authenticated user's unread notification count to all templates.

    Used by the bell component's initial render so the badge shows the correct count
    before the first HTMX poll fires.
    """
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    return {"unread_count": Notification.objects.filter(recipient=request.user, read_at__isnull=True).count()}
