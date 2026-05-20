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
        successful = ctx.get("successful_count", 0)
        failed = ctx.get("failed_count", 0)
        total = ctx.get("total", successful + failed)

        if failed == 0:
            color, emoji = COLOR_SUCCESS, "✅"
        elif successful == 0:
            color, emoji = COLOR_FAILURE, "❌"
        else:
            color, emoji = COLOR_PARTIAL, "⚠️"

        fields: list[dict] = [
            {"title": "Results", "value": f"✓ {successful} · ✗ {failed} of {total}", "short": True},
            {"title": "Duration", "value": self._fmt_duration(ctx.get("duration_seconds")), "short": True},
        ]
        if owner := ctx.get("trigger_owner"):
            fields.append({"title": "Owner", "value": owner, "short": True})
        if (usage := self._usage_field(ctx)) is not None:
            fields.append(usage)
        if (cost := self._cost_field(ctx)) is not None:
            fields.append({"title": "Total cost", "value": cost["value"], "short": True})
        if breakdown := self._repo_breakdown(ctx.get("repo_results") or []):
            fields.append({"title": "Repositories", "value": breakdown, "short": False})

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
    def _repo_breakdown(repo_results: list[dict]) -> str:
        """Format per-repo outcomes as ``✓ a/b · ✓ c/d · ✗ e/f`` with overflow truncation."""
        if not repo_results:
            return ""
        head = repo_results[:_REPO_BREAKDOWN_LIMIT]
        parts = [f"{'✓' if r.get('ok') else '✗'} {r.get('repo', '?')}" for r in head]
        overflow = len(repo_results) - len(head)
        if overflow > 0:
            parts.append(f"… and {overflow} more")
        return " · ".join(parts)
