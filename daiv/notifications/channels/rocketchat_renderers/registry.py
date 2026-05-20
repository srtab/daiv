from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from notifications.channels.rocketchat_renderers.base import RocketChatRenderer
    from notifications.choices import EventType

_registry: dict[str, type[RocketChatRenderer]] = {}


def register_renderer(cls: type[RocketChatRenderer]) -> type[RocketChatRenderer]:
    """Decorator — registers a Rocket Chat renderer under its ``event_type``."""
    event_type = cls.event_type
    if event_type in _registry:
        raise ValueError(f"Rocket Chat renderer for {event_type!r} already registered")
    _registry[event_type] = cls
    return cls


def get_renderer(event_type: str | EventType) -> RocketChatRenderer | None:
    """Return a renderer instance for ``event_type``, or ``None`` if no renderer is registered."""
    cls = _registry.get(event_type)
    return cls() if cls is not None else None
