from __future__ import annotations

from typing import TYPE_CHECKING

from notifications.channels.telegram_renderers.base import TelegramRenderer
from notifications.channels.telegram_renderers.registry import register_renderer
from notifications.choices import EventType

if TYPE_CHECKING:
    from notifications.models import Notification


@register_renderer
class ScheduleFinishedRenderer(TelegramRenderer):
    event_type = EventType.SCHEDULE_FINISHED

    def render(self, notification: Notification) -> tuple[str, dict]:
        ctx = notification.context
        rows: list[tuple[str, str]] = [
            ("Repository", ctx.get("repo_id") or "—"),
            ("Owner", ctx.get("trigger_owner") or "—"),
            ("Duration", self._fmt_duration(ctx.get("duration_seconds"))),
        ]
        rows.extend(self._usage_rows(ctx))
        return self._assemble(notification, ctx, rows)
