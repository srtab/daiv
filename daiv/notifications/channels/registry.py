from __future__ import annotations

from typing import TYPE_CHECKING

from notifications.exceptions import UnknownChannelError

if TYPE_CHECKING:
    from notifications.channels.base import NotificationChannel
    from notifications.choices import ChannelType

_registry: dict[str, type[NotificationChannel]] = {}


def register_channel(cls: type[NotificationChannel]) -> type[NotificationChannel]:
    """Decorator — registers a channel class under its ``channel_type``."""
    channel_type = cls.channel_type
    if channel_type in _registry:
        raise ValueError(f"Channel {channel_type!r} already registered")
    _registry[channel_type] = cls
    return cls


def get_channel(channel_type: str) -> NotificationChannel:
    try:
        return _registry[channel_type]()
    except KeyError as exc:
        raise UnknownChannelError(channel_type) from exc


def all_channels() -> list[type[NotificationChannel]]:
    return list(_registry.values())


def enabled_channels() -> list[type[NotificationChannel]]:
    return [cls for cls in _registry.values() if cls.is_enabled()]


def enabled_channel_types() -> list[ChannelType]:
    """The channel types every emitter delivers on — the one place that list is derived."""
    return [cls.channel_type for cls in enabled_channels()]


def is_registered(channel_type: str) -> bool:
    """Return True if the given channel_type is registered."""
    return channel_type in _registry
