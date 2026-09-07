from __future__ import annotations

from typing import TYPE_CHECKING

from notifications.channels.telegram_renderers.base import TelegramRenderer
from notifications.channels.telegram_renderers.registry import register_renderer
from notifications.choices import EventType

if TYPE_CHECKING:
    from notifications.models import Notification


@register_renderer
class JobFinishedRenderer(TelegramRenderer):
    event_type = EventType.JOB_FINISHED

    def render(self, notification: Notification) -> tuple[str, dict]:
        ctx = notification.context
        rows: list[tuple[str, str]] = [
            ("Trigger", ctx.get("trigger_label") or "—"),
            ("Duration", self._fmt_duration(ctx.get("duration_seconds"))),
        ]
        rows.extend(self._usage_rows(ctx))
        return self._assemble(notification, ctx, rows)
