from __future__ import annotations

from typing import TYPE_CHECKING

from notifications.channels.rocketchat_renderers.base import FOOTER, RocketChatRenderer
from notifications.channels.rocketchat_renderers.registry import register_renderer
from notifications.choices import EventType

if TYPE_CHECKING:
    from notifications.models import Notification


@register_renderer
class ScheduleFinishedRenderer(RocketChatRenderer):
    event_type = EventType.SCHEDULE_FINISHED

    def render(self, notification: Notification) -> tuple[str, list[dict]]:
        ctx = notification.context
        ok = ctx.get("is_successful", False)
        emoji = "✅" if ok else "❌"

        fields: list[dict] = [
            {"title": "Repository", "value": ctx.get("repo_id") or "—", "short": True},
            {"title": "Owner", "value": ctx.get("trigger_owner") or "—", "short": True},
            {"title": "Duration", "value": self._fmt_duration(ctx.get("duration_seconds")), "short": True},
        ]
        if (usage := self._usage_field(ctx)) is not None:
            fields.append(usage)
        if (cost := self._cost_field(ctx)) is not None:
            fields.append(cost)

        attachment = {
            "color": self._color(ctx),
            "title": notification.subject,
            "title_link": self._link(notification),
            "fields": fields,
            "footer": FOOTER,
            "ts": int(notification.created.timestamp()),
        }
        return f"{emoji} {notification.subject}", [attachment]
