from __future__ import annotations

from typing import TYPE_CHECKING

from notifications.channels.telegram_renderers.base import TelegramRenderer
from notifications.channels.telegram_renderers.registry import register_renderer
from notifications.choices import EventType

if TYPE_CHECKING:
    from notifications.models import Notification


@register_renderer
class JobBatchFinishedRenderer(TelegramRenderer):
    event_type = EventType.JOB_BATCH_FINISHED

    def render(self, notification: Notification) -> tuple[str, dict]:
        ctx = notification.context
        notable = ctx.get("notable_count", 0)
        total = ctx.get("total", 0)
        rows: list[tuple[str, str]] = [
            ("Results", f"⚑ {notable} · ✓ {ctx.get('all_clear_count', 0)} of {total}"),
            (
                "Breakdown",
                f"found {ctx.get('found_count', 0)} · needs {ctx.get('needs_attention_count', 0)}"
                f" · failed {ctx.get('failed_count', 0)}",
            ),
            ("Duration", self._fmt_duration(ctx.get("duration_seconds"))),
        ]
        if owner := ctx.get("trigger_owner"):
            rows.append(("Owner", owner))
        rows.extend(self._usage_rows(ctx, cost_label="Total cost"))

        # The repository list is the one region whose length is not bounded by construction,
        # so it goes through ``extra`` and absorbs whatever the 4096 budget has left.
        extra = None
        if repo_ids := ctx.get("repo_ids"):
            extra = ("Repositories", self.esc(self._repo_list(repo_ids)))
        return self._assemble(notification, ctx, rows, extra=extra)
