from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

from core.utils import build_absolute_url

if TYPE_CHECKING:
    from notifications.choices import EventType
    from notifications.models import Notification


# Slack-compatible attachment colors (Rocket Chat accepts hex).
COLOR_SUCCESS = "#22c55e"  # green
COLOR_FAILURE = "#ef4444"  # red
COLOR_PARTIAL = "#eab308"  # yellow

# (color, emoji) per tone, keyed by the ``status_tone`` the notifiers stamp on the context.
TONE_STYLE = {"success": (COLOR_SUCCESS, "✅"), "failure": (COLOR_FAILURE, "❌"), "warning": (COLOR_PARTIAL, "⚠️")}

FOOTER = "DAIV"


class RocketChatRenderer(ABC):
    """Base for per-event Rocket Chat attachment renderers.

    Subclasses set ``event_type`` and implement ``render``. They are registered
    with the ``@register_renderer`` decorator from ``.registry``.
    """

    event_type: ClassVar[EventType]

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        # Concrete renderers must declare ``event_type`` so the registry can key them and
        # so a forgotten assignment surfaces at import time rather than inside chat.postMessage.
        if not inspect.isabstract(cls) and "event_type" not in cls.__dict__:
            raise TypeError(f"{cls.__name__} must define `event_type` (a notifications.choices.EventType value)")

    @abstractmethod
    def render(self, notification: Notification) -> tuple[str, list[dict]]:
        """Return ``(text, attachments)`` to send via Rocket Chat's ``chat.postMessage``."""

    @staticmethod
    def _fmt_tokens(n: int | None) -> str | None:
        if n is None:
            return None
        if n < 1000:
            return str(n)
        return f"{n / 1000:.1f}k"

    @staticmethod
    def _fmt_cost(usd: float | None) -> str | None:
        if usd is None:
            return None
        return f"${usd:.2f}"

    @staticmethod
    def _fmt_duration(seconds: float | None) -> str:
        if seconds is None:
            return "—"
        total = int(seconds)
        if total < 60:
            return f"{total}s"
        if total < 3600:
            return f"{total // 60}m {total % 60:02d}s"
        return f"{total // 3600}h {(total % 3600) // 60:02d}m"

    @staticmethod
    def _usage_field(ctx: dict) -> dict | None:
        """Combine input/output tokens into one short field; ``None`` if both are missing."""
        in_tokens = RocketChatRenderer._fmt_tokens(ctx.get("input_tokens"))
        out_tokens = RocketChatRenderer._fmt_tokens(ctx.get("output_tokens"))
        if in_tokens is None and out_tokens is None:
            return None
        return {"title": "Usage", "value": f"{in_tokens or '—'} in · {out_tokens or '—'} out", "short": True}

    @staticmethod
    def _cost_field(ctx: dict) -> dict | None:
        """One-field cost, or ``None`` when no cost data is available."""
        cost = RocketChatRenderer._fmt_cost(ctx.get("cost_usd"))
        if cost is None:
            return None
        return {"title": "Cost", "value": cost, "short": True}

    @staticmethod
    def _link(notification: Notification) -> str:
        return build_absolute_url(notification.link_url) if notification.link_url else ""

    @staticmethod
    def _findings_field(ctx: dict) -> dict | None:
        """The classifier's findings as one long field; ``None`` when the run listed none."""
        items = ctx.get("actionable") or []
        if not items:
            return None
        lines = []
        for item in items:
            prefix = f"[{item['kind']}] " if item.get("kind") else ""
            ref = f" — `{item['ref']}`" if item.get("ref") else ""
            lines.append(f"• {prefix}{item.get('label', '')}{ref}")
        overflow = ctx.get("actionable_overflow") or 0
        if overflow:
            lines.append(f"… and {overflow} more")
        return {"title": "Findings", "value": "\n".join(lines), "short": False}

    def _message(
        self, notification: Notification, color: str, emoji: str, fields: list[dict]
    ) -> tuple[str, list[dict]]:
        """Assemble the shared ``(text, [attachment])`` shape every renderer returns.

        The classifier's summary and findings ride along whenever the context carries them, so no
        renderer can ship without the reason a human was paged.
        """
        ctx = notification.context
        if (findings := self._findings_field(ctx)) is not None:
            fields = [*fields, findings]
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
        """(color, emoji) for a single run, from the envelope-driven ``status_tone``. Falls back to
        the legacy ``is_successful`` when a context predates the tone key."""
        tone = ctx.get("status_tone")
        if tone in TONE_STYLE:
            return TONE_STYLE[tone]
        return TONE_STYLE["success"] if ctx.get("is_successful") else TONE_STYLE["failure"]
