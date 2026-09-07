from __future__ import annotations

from typing import TYPE_CHECKING

from notifications.channels.renderers.registry import RendererRegistry

if TYPE_CHECKING:
    from notifications.channels.rocketchat_renderers.base import RocketChatRenderer

_registry: RendererRegistry[RocketChatRenderer] = RendererRegistry("Rocket Chat")

register_renderer = _registry.register
get_renderer = _registry.get
