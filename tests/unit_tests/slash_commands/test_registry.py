import pytest

from codebase.base import Scope
from slash_commands.base import SlashCommand
from slash_commands.registry import SlashCommandRegistry


class MockCommand1(SlashCommand):
    @property
    def description(self):
        return "Mock command 1"

    def execute(self, repo_id, scope, note, user, issue=None, merge_request=None, args=None):
        pass


class MockCommand2(SlashCommand):
    @property
    def description(self):
        return "Mock command 2"

    def execute(self, repo_id, scope, note, user, issue=None, merge_request=None, args=None):
        pass


class NotACommand:
    """Class that doesn't inherit from SlashCommand."""

    pass


class TestSlashCommandRegistry:
    def test_registry_initialization(self):
        """Test that registry initializes with empty state."""
        registry = SlashCommandRegistry()
        assert registry._registry == {}
        assert registry._registry_by_scope == {}

    def test_register_valid_command(self):
        """Test registering a valid slash command."""
        registry = SlashCommandRegistry()
        scopes = [Scope.ISSUE, Scope.MERGE_REQUEST]

        registry.register(MockCommand1, "test_command", scopes)

        assert "test_command" in registry._registry
        assert registry._registry["test_command"] == MockCommand1
        assert getattr(MockCommand1, "command", None) == "test_command"
        assert getattr(MockCommand1, "scopes", None) == scopes
        assert MockCommand1 in registry._registry_by_scope[Scope.ISSUE.value]
        assert MockCommand1 in registry._registry_by_scope[Scope.MERGE_REQUEST.value]

    def test_register_invalid_class_raises_assertion(self):
        """Test that registering non-SlashCommand class raises AssertionError."""
        registry = SlashCommandRegistry()

        with pytest.raises(AssertionError, match="must be a class that inherits from SlashCommand"):
            registry.register(NotACommand, "invalid", [Scope.ISSUE])

    def test_register_non_class_raises_assertion(self):
        """Test that registering non-class raises AssertionError."""
        registry = SlashCommandRegistry()

        with pytest.raises(AssertionError, match="must be a class that inherits from SlashCommand"):
            registry.register("not_a_class", "invalid", [Scope.ISSUE])

    def test_register_duplicate_command_class_raises_assertion(self):
        """Test that registering same command class twice raises AssertionError."""
        registry = SlashCommandRegistry()

        registry.register(MockCommand1, "first_command", [Scope.ISSUE])

        with pytest.raises(AssertionError, match="is already registered"):
            registry.register(MockCommand1, "second_command", [Scope.ISSUE])

    def test_register_duplicate_command_raises_assertion(self):
        """Test that registering same command twice raises AssertionError."""
        registry = SlashCommandRegistry()

        registry.register(MockCommand1, "duplicate_command", [Scope.ISSUE])

        with pytest.raises(AssertionError, match="is already registered"):
            registry.register(MockCommand2, "duplicate_command", [Scope.ISSUE])

    def test_register_multiple_scopes(self):
        """Test registering command with multiple scopes."""
        registry = SlashCommandRegistry()
        scopes = [Scope.ISSUE, Scope.MERGE_REQUEST]

        registry.register(MockCommand1, "multi_scope", scopes)

        assert MockCommand1 in registry._registry_by_scope[Scope.ISSUE.value]
        assert MockCommand1 in registry._registry_by_scope[Scope.MERGE_REQUEST.value]

    def test_register_single_scope(self):
        """Test registering command with single scope."""
        registry = SlashCommandRegistry()
        scopes = [Scope.ISSUE]

        registry.register(MockCommand1, "single_scope", scopes)

        assert MockCommand1 in registry._registry_by_scope[Scope.ISSUE.value]
        assert (
            Scope.MERGE_REQUEST.value not in registry._registry_by_scope
            or MockCommand1 not in registry._registry_by_scope[Scope.MERGE_REQUEST.value]
        )

    def test_get_commands_no_filters(self):
        """Test getting all commands without filters."""
        registry = SlashCommandRegistry()

        registry.register(MockCommand1, "command1", [Scope.ISSUE])
        registry.register(MockCommand2, "command2", [Scope.MERGE_REQUEST])

        commands = registry.get_commands()

        assert len(commands) == 2
        assert MockCommand1 in commands
        assert MockCommand2 in commands

    def test_get_commands_by_command_only(self):
        """Test getting commands by command only."""
        registry = SlashCommandRegistry()

        registry.register(MockCommand1, "command1", [Scope.ISSUE])
        registry.register(MockCommand2, "command2", [Scope.MERGE_REQUEST])

        commands = registry.get_commands(command="command1")

        assert len(commands) == 1
        assert commands[0] == MockCommand1

    def test_get_commands_by_command_not_found(self):
        """Test getting commands by non-existent command."""
        registry = SlashCommandRegistry()

        registry.register(MockCommand1, "command1", [Scope.ISSUE])

        commands = registry.get_commands(command="nonexistent")

        assert len(commands) == 0

    def test_get_commands_by_scope_only(self):
        """Test getting commands by scope only."""
        registry = SlashCommandRegistry()

        registry.register(MockCommand1, "command1", [Scope.ISSUE])
        registry.register(MockCommand2, "command2", [Scope.MERGE_REQUEST])

        commands = registry.get_commands(scope=Scope.ISSUE)

        assert len(commands) == 1
        assert commands[0] == MockCommand1

    def test_get_commands_by_scope_not_found(self):
        """Test getting commands by scope with no registered commands."""
        registry = SlashCommandRegistry()

        commands = registry.get_commands(scope=Scope.ISSUE)

        assert len(commands) == 0

    def test_get_commands_by_command_and_scope(self):
        """Test getting commands by both command and scope."""
        registry = SlashCommandRegistry()

        registry.register(MockCommand1, "command1", [Scope.ISSUE, Scope.MERGE_REQUEST])
        registry.register(MockCommand2, "command2", [Scope.ISSUE])

        commands = registry.get_commands(command="command1", scope=Scope.ISSUE)

        assert len(commands) == 1
        assert commands[0] == MockCommand1

    def test_get_commands_by_command_and_scope_not_found(self):
        """Test getting commands by command and scope with no matches."""
        registry = SlashCommandRegistry()

        registry.register(MockCommand1, "command1", [Scope.ISSUE])

        commands = registry.get_commands(command="command1", scope=Scope.MERGE_REQUEST)

        assert len(commands) == 0

    def test_get_commands_multiple_commands_same_scope(self):
        """Test getting multiple commands for same scope."""
        registry = SlashCommandRegistry()

        registry.register(MockCommand1, "command1", [Scope.ISSUE])
        registry.register(MockCommand2, "command2", [Scope.ISSUE])

        commands = registry.get_commands(scope=Scope.ISSUE)

        assert len(commands) == 2
        assert MockCommand1 in commands
        assert MockCommand2 in commands

    def test_command_attributes_set_correctly(self):
        """Test that command class attributes are set correctly during registration."""
        registry = SlashCommandRegistry()
        original_command = getattr(MockCommand1, "command", None)
        original_scopes = getattr(MockCommand1, "scopes", None)

        try:
            scopes = [Scope.ISSUE, Scope.MERGE_REQUEST]
            registry.register(MockCommand1, "test_attributes", scopes)

            assert hasattr(MockCommand1, "command") and MockCommand1.command == "test_attributes"
            assert hasattr(MockCommand1, "scopes") and MockCommand1.scopes == scopes
        finally:
            # Clean up - restore original attributes if they existed
            if original_command is not None:
                MockCommand1.command = original_command
            elif hasattr(MockCommand1, "command"):
                delattr(MockCommand1, "command")

            if original_scopes is not None:
                MockCommand1.scopes = original_scopes
            elif hasattr(MockCommand1, "scopes"):
                delattr(MockCommand1, "scopes")
