from typing import TYPE_CHECKING

from .registry import quick_action_registry

if TYPE_CHECKING:
    from collections.abc import Callable

    from .base import QuickAction, Scope


def quick_action(command: str, scopes: list[Scope]) -> Callable[[type[QuickAction]], type[QuickAction]]:
    """
    Decorator to register a quick action.

    Usage:
        @quick_action(command="my_action", scopes=[Scopes.ISSUE, Scopes.MERGE_REQUEST])
        class MyAction(QuickAction):
            # ... implementation

    Args:
        cls: The quick action class to register.

    Returns:
        The quick action class.
    """

    def decorator(cls: type[QuickAction]) -> type[QuickAction]:
        quick_action_registry.register(cls, command, scopes)
        return cls

    return decorator
