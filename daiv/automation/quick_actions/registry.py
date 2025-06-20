from __future__ import annotations

from inspect import isclass

from .base import QuickAction, Scope


class QuickActionRegistry:
    """
    Registry that keeps track of the registered quick actions.
    """

    def __init__(self):
        self._registry: dict[str, type[QuickAction]] = {}
        self._registry_by_scope: dict[str, list[type[QuickAction]]] = {}

    def register(self, action: type[QuickAction], verb: str, scopes: list[Scope]) -> None:
        """
        Register a quick action class.

        Args:
            action: The quick action class to register.
            verb: The verb to register the action with.
            scopes: The scopes to register the action for.
        """
        assert isclass(action) and issubclass(action, QuickAction), (
            f"{action} must be a class that inherits from QuickAction"
        )
        assert action not in self._registry.values(), f"{action.__name__} is already registered as quick action."
        assert verb not in self._registry, f"{verb} is already registered as quick action."

        action.verb = verb  # type: ignore
        action.scopes = scopes  # type: ignore

        self._registry[verb] = action
        for scope in scopes:
            if scope.value not in self._registry_by_scope:
                self._registry_by_scope[scope.value] = []
            self._registry_by_scope[scope.value].append(action)

    def get_actions(self, scope: Scope | None = None, verb: str | None = None) -> list[type[QuickAction]]:
        """
        Get quick actions that support the given scope.

        Args:
            scope: The scope to get quick actions for.
            verb: The verb to get quick actions for.

        Returns:
            List of quick action classes that support the given scope.
        """
        if scope is None and verb is None:
            return list(self._registry.values())
        if scope is None:
            if action := self._registry.get(verb, None):
                return [action]
            return []
        if verb is None:
            return self._registry_by_scope.get(scope.value, [])
        return list(filter(lambda x: x.verb == verb, self._registry_by_scope.get(scope.value, [])))


quick_action_registry = QuickActionRegistry()
