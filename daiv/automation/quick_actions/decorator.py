from functools import wraps

from .base import QuickAction
from .registry import quick_action_registry


def quick_action(cls: type[QuickAction]) -> type[QuickAction]:
    """
    Decorator to register a quick action.

    Usage:
        @quick_action
        class MyAction(QuickAction):
            identifier = "my_action"
            # ... implementation

    Args:
        cls: The quick action class to register.

    Returns:
        The quick action class.
    """
    quick_action_registry.register(cls)

    @wraps(cls)
    def wrapper(*args, **kwargs) -> QuickAction:
        return cls(*args, **kwargs)

    return wrapper