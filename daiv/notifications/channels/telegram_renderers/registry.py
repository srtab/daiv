from __future__ import annotations

from typing import TYPE_CHECKING

from notifications.channels.renderers.registry import RendererRegistry

if TYPE_CHECKING:
    from notifications.channels.telegram_renderers.base import TelegramRenderer

_registry: RendererRegistry[TelegramRenderer] = RendererRegistry("Telegram")

register_renderer = _registry.register
get_renderer = _registry.get
