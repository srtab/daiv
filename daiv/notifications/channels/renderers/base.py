"""Format-agnostic renderer helpers shared by every chat channel.

Tone is channel-neutral by design: the notifiers stamp ``context["status_tone"]`` and every
channel reads it rather than re-deriving tone from ``status``. This module owns the tone *key*
and the tone → emoji map; per-channel styling (Rocket Chat's hex colours) stays with the channel.
"""

from __future__ import annotations

import inspect
from abc import ABC
from typing import TYPE_CHECKING, ClassVar

from core.utils import build_absolute_url

if TYPE_CHECKING:
    from notifications.choices import EventType
    from notifications.models import Notification

# Every channel uses these unchanged.
TONE_EMOJI = {"success": "✅", "failure": "❌", "warning": "⚠️"}

# How many repositories a batch rollup names before collapsing the rest into a count.
REPO_BREAKDOWN_LIMIT = 8


class BaseRenderer(ABC):
    """Base for per-event renderers on any channel.

    Subclasses set ``event_type``, declare their own channel-shaped ``render``, and register
    with their channel's ``@register_renderer``.
    """

    event_type: ClassVar[EventType]

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        # Concrete renderers must declare ``event_type`` so the registry can key them and so a
        # forgotten assignment surfaces at import time rather than mid-delivery.
        if not inspect.isabstract(cls) and "event_type" not in cls.__dict__:
            raise TypeError(f"{cls.__name__} must define `event_type` (a notifications.choices.EventType value)")

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
    def _link(notification: Notification) -> str:
        return build_absolute_url(notification.link_url) if notification.link_url else ""

    @staticmethod
    def _tone(ctx: dict) -> str:
        """The ``{success, warning, failure}`` key for this notification.

        Reads the ``status_tone`` the notifiers stamp, falling back to the legacy
        ``is_successful`` for contexts written before that key existed. No channel should
        re-derive tone from ``status``.
        """
        tone = ctx.get("status_tone")
        if tone in TONE_EMOJI:
            return tone
        return "success" if ctx.get("is_successful") else "failure"

    @staticmethod
    def _repo_list(repo_ids: list[str]) -> str:
        """The batch rollup's repository breakdown, capped at ``REPO_BREAKDOWN_LIMIT``."""
        if not repo_ids:
            return ""
        head = repo_ids[:REPO_BREAKDOWN_LIMIT]
        overflow = len(repo_ids) - len(head)
        parts = list(head)
        if overflow > 0:
            parts.append(f"… and {overflow} more")
        return " · ".join(parts)
