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

    def _message(
        self, notification: Notification, color: str, emoji: str, fields: list[dict]
    ) -> tuple[str, list[dict]]:
        """Assemble the shared ``(text, [attachment])`` shape every renderer returns."""
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
    def _tone_style(ctx: dict) -> tuple[str, str]:
        """``(color, emoji)`` for a notification, from the shared tone key."""
        tone = BaseRenderer._tone(ctx)
        return TONE_COLOR[tone], TONE_EMOJI[tone]
