"""Command registry and base class — mechanism only.

The concrete handlers live in ``notifications/api/telegram_handlers.py`` because they read and
write ``UserChannelBinding``, and this package must not import notification internals. They
register from that side.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from notifications.telegram.schemas import TGChat

_registry: dict[str, type[BaseCommand]] = {}


def register_command(cls: type[BaseCommand]) -> type[BaseCommand]:
    """Decorator — registers a command under its bare ``name`` (no leading slash)."""
    name = cls.name
    if name in _registry:
        raise ValueError(f"Telegram command {name!r} already registered")
    _registry[name] = cls
    return cls


def get_command(name: str) -> BaseCommand | None:
    """Return a command instance for ``name``, or ``None`` if nothing is registered."""
    cls = _registry.get(name)
    return cls() if cls is not None else None


class BaseCommand(ABC):
    """Base for private-chat bot commands.

    Subclasses set ``name`` and implement ``handle``, and register with ``@register_command``.
    """

    name: ClassVar[str]

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        # Concrete commands must declare ``name`` so a forgotten assignment surfaces at import
        # time rather than as a command that silently never dispatches.
        if not inspect.isabstract(cls) and "name" not in cls.__dict__:
            raise TypeError(f"{cls.__name__} must define `name` (the bare command, without the leading slash)")

    @abstractmethod
    def handle(self, chat: TGChat, argument: str) -> str | None:
        """Handle the command; return reply text, or ``None`` for silence."""
