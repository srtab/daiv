from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from notifications.channels.telegram_renderers.base import TelegramRenderer
    from notifications.choices import EventType

_registry: dict[str, type[TelegramRenderer]] = {}


def register_renderer(cls: type[TelegramRenderer]) -> type[TelegramRenderer]:
    """Decorator — registers a Telegram renderer under its ``event_type``."""
    event_type = cls.event_type
    if event_type in _registry:
        raise ValueError(f"Telegram renderer for {event_type!r} already registered")
    _registry[event_type] = cls
    return cls


def get_renderer(event_type: str | EventType) -> TelegramRenderer | None:
    """Return a renderer instance for ``event_type``, or ``None`` if none is registered."""
    cls = _registry.get(event_type)
    return cls() if cls is not None else None
