from __future__ import annotations

from typing import TYPE_CHECKING

from notifications.channels.rocketchat_renderers.base import (
    COLOR_FAILURE,
    COLOR_PARTIAL,
    COLOR_SUCCESS,
    FOOTER,
    RocketChatRenderer,
)
from notifications.channels.rocketchat_renderers.registry import register_renderer
from notifications.choices import EventType

if TYPE_CHECKING:
    from notifications.models import Notification


_REPO_BREAKDOWN_LIMIT = 8


@register_renderer
class JobBatchFinishedRenderer(RocketChatRenderer):
    event_type = EventType.JOB_BATCH_FINISHED

    def render(self, notification: Notification) -> tuple[str, list[dict]]:
        ctx = notification.context
        notable = ctx.get("notable_count", 0)
        total = ctx.get("total", 0)
        failed = ctx.get("failed_count", 0)
        found = ctx.get("found_count", 0)
        needs = ctx.get("needs_attention_count", 0)
        clear = ctx.get("all_clear_count", 0)

        if notable == 0:
            color, emoji = COLOR_SUCCESS, "✅"
        elif notable == total:
            color, emoji = COLOR_FAILURE, "❌"
        else:
            color, emoji = COLOR_PARTIAL, "⚠️"

        fields: list[dict] = [
            {"title": "Results", "value": f"⚑ {notable} · ✓ {clear} of {total}", "short": True},
            {"title": "Breakdown", "value": f"found {found} · needs {needs} · failed {failed}", "short": True},
            {"title": "Duration", "value": self._fmt_duration(ctx.get("duration_seconds")), "short": True},
        ]
        if owner := ctx.get("trigger_owner"):
            fields.append({"title": "Owner", "value": owner, "short": True})
        if (usage := self._usage_field(ctx)) is not None:
            fields.append(usage)
        if (cost := self._cost_field(ctx)) is not None:
            fields.append({"title": "Total cost", "value": cost["value"], "short": True})
        if repo_ids := ctx.get("repo_ids"):
            fields.append({"title": "Repositories", "value": self._repo_list(repo_ids), "short": False})

        attachment = {
            "color": color,
            "title": notification.subject,
            "title_link": self._link(notification),
            "fields": fields,
            "footer": FOOTER,
            "ts": int(notification.created.timestamp()),
        }
        return f"{emoji} {notification.subject}", [attachment]

    @staticmethod
    def _repo_list(repo_ids: list[str]) -> str:
        if not repo_ids:
            return ""
        head = repo_ids[:_REPO_BREAKDOWN_LIMIT]
        overflow = len(repo_ids) - len(head)
        parts = list(head)
        if overflow > 0:
            parts.append(f"… and {overflow} more")
        return " · ".join(parts)
