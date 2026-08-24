"""The per-channel ``event_type`` → renderer map.

One instance per channel: a renderer registered for Rocket Chat must never satisfy a Telegram
lookup, so the registries stay separate objects rather than one map keyed on both.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from notifications.channels.renderers.base import BaseRenderer
    from notifications.choices import EventType


class RendererRegistry[R: BaseRenderer]:
    """``channel_label`` names the channel in the duplicate-registration error, nothing else."""

    def __init__(self, channel_label: str) -> None:
        self.channel_label = channel_label
        self._renderers: dict[str, type[R]] = {}

    def register(self, cls: type[R]) -> type[R]:
        """Decorator — registers a renderer under its ``event_type``."""
        if cls.event_type in self._renderers:
            raise ValueError(f"{self.channel_label} renderer for {cls.event_type!r} already registered")
        self._renderers[cls.event_type] = cls
        return cls

    def get(self, event_type: str | EventType) -> R | None:
        """Return a renderer instance for ``event_type``, or ``None`` if none is registered."""
        cls = self._renderers.get(event_type)
        return cls() if cls is not None else None
