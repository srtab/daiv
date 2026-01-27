from typing import TYPE_CHECKING

from .registry import slash_command_registry

if TYPE_CHECKING:
    from collections.abc import Callable

    from codebase.base import Scope

    from .base import SlashCommand


def slash_command(command: str, scopes: list[Scope]) -> Callable[[type[SlashCommand]], type[SlashCommand]]:
    """
    Decorator to register a slash command.

    Usage:
        @slash_command(command="my_command", scopes=[Scopes.ISSUE, Scopes.MERGE_REQUEST])
        class MyCommand(SlashCommand):
            # ... implementation

    Args:
        cls: The slash command class to register.

    Returns:
        The slash command class.
    """

    def decorator(cls: type[SlashCommand]) -> type[SlashCommand]:
        slash_command_registry.register(cls, command, scopes)
        return cls

    return decorator
