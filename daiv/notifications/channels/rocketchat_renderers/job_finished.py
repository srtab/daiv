from __future__ import annotations

from typing import TYPE_CHECKING

from notifications.channels.rocketchat_renderers.base import RocketChatRenderer
from notifications.channels.rocketchat_renderers.registry import register_renderer
from notifications.choices import EventType

if TYPE_CHECKING:
    from notifications.models import Notification


@register_renderer
class JobFinishedRenderer(RocketChatRenderer):
    event_type = EventType.JOB_FINISHED

    def render(self, notification: Notification) -> tuple[str, list[dict]]:
        ctx = notification.context
        color, emoji = self._tone_style(ctx)

        fields: list[dict] = [
            {"title": "Trigger", "value": ctx.get("trigger_label") or "—", "short": True},
            {"title": "Duration", "value": self._fmt_duration(ctx.get("duration_seconds")), "short": True},
        ]
        if (usage := self._usage_field(ctx)) is not None:
            fields.append(usage)
        if (cost := self._cost_field(ctx)) is not None:
            fields.append(cost)

        return self._message(notification, color, emoji, fields)
