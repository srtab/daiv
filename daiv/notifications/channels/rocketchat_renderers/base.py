from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING

from notifications.channels.renderers.base import COST_LABEL, TONE_EMOJI, USAGE_LABEL, BaseRenderer

if TYPE_CHECKING:
    from notifications.models import Notification


# Slack-compatible attachment colors (Rocket Chat accepts hex).
COLOR_SUCCESS = "#22c55e"  # green
COLOR_FAILURE = "#ef4444"  # red
COLOR_PARTIAL = "#eab308"  # yellow

# Rocket Chat-specific half of the styling: the tone → hex map. The tone *key* and the
# tone → emoji map are shared, so no channel can disagree about which outcome is a warning.
TONE_COLOR = {"success": COLOR_SUCCESS, "failure": COLOR_FAILURE, "warning": COLOR_PARTIAL}

FOOTER = "DAIV"


class RocketChatRenderer(BaseRenderer):
    """Base for per-event Rocket Chat attachment renderers.

    Subclasses set ``event_type`` and implement ``render``. They are registered with the
    ``@register_renderer`` decorator from ``.registry``.
    """

    @abstractmethod
    def render(self, notification: Notification) -> tuple[str, list[dict]]:
        """Return ``(text, attachments)`` to send via Rocket Chat's ``chat.postMessage``."""

    @staticmethod
    def _usage_field(ctx: dict) -> dict | None:
        """Combine input/output tokens into one short field; ``None`` if both are missing."""
        value = BaseRenderer._usage_value(ctx)
        return None if value is None else {"title": USAGE_LABEL, "value": value, "short": True}

    @staticmethod
    def _cost_field(ctx: dict, *, label: str = COST_LABEL) -> dict | None:
        """One-field cost, or ``None`` when no cost data is available."""
        value = BaseRenderer._cost_value(ctx)
        return None if value is None else {"title": label, "value": value, "short": True}

    @staticmethod
    def _list_field(title: str, items: list[dict] | None, overflow: int, *, mono: bool = True) -> dict | None:
        """One long field listing ``{kind, label, ref}`` rows; ``None`` when there are none.

        ``mono`` backticks ``ref`` — right for a file path, wrong for the prose a batch row carries.
        """
        if not items:
            return None
        lines = []
        for item in items:
            prefix = f"[{item['kind']}] " if item.get("kind") else ""
            ref = item.get("ref")
            tail = (f" — `{ref}`" if mono else f" — {ref}") if ref else ""
            lines.append(f"• {prefix}{item.get('label', '')}{tail}")
        if overflow > 0:
            lines.append(f"… and {overflow} more")
        return {"title": title, "value": "\n".join(lines), "short": False}

    def _message(
        self, notification: Notification, color: str, emoji: str, fields: list[dict]
    ) -> tuple[str, list[dict]]:
        """Assemble the shared ``(text, [attachment])`` shape every renderer returns.

        Context-derived fields are attached here, not per renderer, so a new renderer cannot omit them.
        """
        ctx = notification.context
        extra = (
            self._list_field("Findings", ctx.get("actionable"), ctx.get("actionable_overflow") or 0),
            self._list_field(
                "Needs a look", ctx.get("notable_runs"), ctx.get("notable_runs_overflow") or 0, mono=False
            ),
        )
        fields = [*fields, *(field for field in extra if field is not None)]
        attachment = {
            "color": color,
            "title": notification.subject,
            "title_link": self._link(notification),
            "fields": fields,
            "footer": FOOTER,
            "ts": int(notification.created.timestamp()),
        }
        if summary := (ctx.get("summary") or "").strip():
            attachment["text"] = summary
        return f"{emoji} {notification.subject}", [attachment]

    @staticmethod
    def _tone_style(ctx: dict) -> tuple[str, str]:
        """``(color, emoji)`` for a notification, from the shared tone key."""
        tone = BaseRenderer._tone(ctx)
        return TONE_COLOR[tone], TONE_EMOJI[tone]
