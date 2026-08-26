from __future__ import annotations

from typing import TYPE_CHECKING

from notifications.channels.rocketchat_renderers.base import RocketChatRenderer
from notifications.channels.rocketchat_renderers.registry import register_renderer
from notifications.choices import EventType

if TYPE_CHECKING:
    from notifications.models import Notification


@register_renderer
class PipelineWatchExhaustedRenderer(RocketChatRenderer):
    event_type = EventType.PIPELINE_WATCH_EXHAUSTED

    def render(self, notification: Notification) -> tuple[str, list[dict]]:
        ctx = notification.context
        color, emoji = self._tone_style(ctx)

        fields: list[dict] = [{"title": "Repository", "value": ctx.get("repo_id") or "—", "short": True}]
        if iid := ctx.get("merge_request_iid"):
            fields.append({"title": "Merge request", "value": f"!{iid}", "short": True})
        fields.append({"title": "Attempts", "value": str(ctx.get("attempts") or "—"), "short": True})
        if jobs := ctx.get("failing_jobs"):
            fields.append({"title": "Failing jobs", "value": ", ".join(jobs), "short": False})
        if pipeline_url := ctx.get("pipeline_url"):
            # Already absolute (a CI host URL), so it must not go through _link().
            fields.append({"title": "Pipeline", "value": pipeline_url, "short": False})

        return self._message(notification, color, emoji, fields)
