from __future__ import annotations

from inspect import isclass
from typing import TYPE_CHECKING

from .base import QuickAction

if TYPE_CHECKING:
    pass


class QuickActionRegistry:
    """
    Registry that keeps track of the registered quick actions.
    """

    def __init__(self):
        self._registry: list[type[QuickAction]] = []

    def register(self, action: type[QuickAction]) -> None:
        """
        Register a quick action class.

        Args:
            action: The quick action class to register.

        Raises:
            AssertionError: If the action is invalid or already registered.
        """
        assert isclass(action) and issubclass(action, QuickAction), (
            f"{action} must be a class that inherits from QuickAction"
        )
        assert action not in self._registry, f"{action.__name__} is already registered as quick action."

        self._registry.append(action)

    def get_actions_for_context(self, is_issue: bool = False, is_merge_request: bool = False) -> list[type[QuickAction]]:
        """
        Get quick actions that support the given context.

        Args:
            is_issue: Whether the context is an issue.
            is_merge_request: Whether the context is a merge request.

        Returns:
            List of quick action classes that support the context.
        """
        return [
            action_class
            for action_class in self._registry
            if (is_issue and action_class.supports_issues) or (is_merge_request and action_class.supports_merge_requests)
        ]

    def get_action_by_identifier(self, identifier: str) -> type[QuickAction] | None:
        """
        Get a quick action by its identifier.

        Args:
            identifier: The identifier to search for.

        Returns:
            The quick action class if found, None otherwise.
        """
        for action_class in self._registry:
            if action_class.identifier == identifier:
                return action_class
        return None

    def get_all_actions(self) -> list[type[QuickAction]]:
        """
        Get all registered quick actions.

        Returns:
            List of all registered quick action classes.
        """
        return self._registry.copy()


quick_action_registry = QuickActionRegistry()