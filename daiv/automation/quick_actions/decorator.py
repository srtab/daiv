from collections.abc import Callable

from .base import QuickAction, Scope
from .registry import quick_action_registry


def quick_action(verb: str, scopes: list[Scope]) -> Callable[[type[QuickAction]], type[QuickAction]]:
    """
    Decorator to register a quick action.

    Usage:
        @quick_action(verb="my_action", scopes=[Scopes.ISSUE, Scopes.MERGE_REQUEST])
        class MyAction(QuickAction):
            # ... implementation

    Args:
        cls: The quick action class to register.

    Returns:
        The quick action class.
    """

    def decorator(cls: type[QuickAction]) -> type[QuickAction]:
        quick_action_registry.register(cls, verb, scopes)
        return cls

    return decorator
