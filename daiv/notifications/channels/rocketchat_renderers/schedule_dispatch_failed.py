from __future__ import annotations

from typing import TYPE_CHECKING

from notifications.channels.rocketchat_renderers.base import RocketChatRenderer
from notifications.channels.rocketchat_renderers.registry import register_renderer
from notifications.choices import EventType

if TYPE_CHECKING:
    from notifications.models import Notification


@register_renderer
class ScheduleDispatchFailedRenderer(RocketChatRenderer):
    event_type = EventType.SCHEDULE_DISPATCH_FAILED

    def render(self, notification: Notification) -> tuple[str, list[dict]]:
        ctx = notification.context
        color, emoji = self._tone_style(ctx)

        repo_ids = ctx.get("repo_ids") or []
        fields: list[dict] = [
            {
                "title": "Repositories" if len(repo_ids) > 1 else "Repository",
                "value": self._repo_list(repo_ids) or "—",
                "short": True,
            },
            {"title": "Last run", "value": ctx.get("last_run") or "—", "short": True},
            {"title": "Reason", "value": ctx.get("reason") or "—", "short": False},
        ]
        return self._message(notification, color, emoji, fields)
