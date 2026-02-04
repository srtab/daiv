from __future__ import annotations

from inspect import isclass
from typing import TYPE_CHECKING

from .base import SlashCommand

if TYPE_CHECKING:
    from codebase.base import Scope


class SlashCommandRegistry:
    """
    Registry that keeps track of the registered slash commands.
    """

    def __init__(self):
        self._registry: dict[str, type[SlashCommand]] = {}
        self._registry_by_scope: dict[str, list[type[SlashCommand]]] = {}

    def register(self, command_cls: type[SlashCommand], command: str, scopes: list[Scope]) -> None:
        """
        Register a slash command class.

        Args:
            command_cls: The slash command class to register.
            command: The command to register the action with.
            scopes: The scopes to register the action for.
        """
        assert isclass(command_cls) and issubclass(command_cls, SlashCommand), (
            f"{command_cls} must be a class that inherits from SlashCommand"
        )
        assert command_cls not in self._registry.values(), f"{command_cls.__name__} is already registered."
        assert command not in self._registry, f"{command} is already registered."

        command_cls.command = command
        command_cls.scopes = scopes

        self._registry[command] = command_cls
        for scope in scopes:
            if scope.value not in self._registry_by_scope:
                self._registry_by_scope[scope.value] = []
            self._registry_by_scope[scope.value].append(command_cls)

    def get_commands(self, scope: Scope | None = None, command: str | None = None) -> list[type[SlashCommand]]:
        """
        Get slash commands that support the given scope.

        Args:
            scope: The scope to get slash commands for.
            command: The command to get slash commands for.

        Returns:
            List of slash command classes that support the given scope.
        """
        if scope is None and command is None:
            return list(self._registry.values())
        if scope is None:
            if command_cls := self._registry.get(command):
                return [command_cls]
            return []
        if command is None:
            return self._registry_by_scope.get(scope.value, [])
        return list(filter(lambda x: x.command == command, self._registry_by_scope.get(scope.value, [])))


slash_command_registry = SlashCommandRegistry()
