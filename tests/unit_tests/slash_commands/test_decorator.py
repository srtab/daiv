from unittest.mock import MagicMock, patch

from codebase.base import Scope
from slash_commands.base import SlashCommand
from slash_commands.decorator import slash_command


class TestSlashCommandDecorator:
    class TestCommand(SlashCommand):
        actions = [MagicMock()]

        async def execute_for_agent(self, *, args: str, **kwargs) -> str:
            return "test_command"

    def test_decorator_with_valid_command(self):
        """Test that decorator properly registers a valid slash command."""
        with patch("slash_commands.decorator.slash_command_registry") as mock_registry:
            slash_command(command="test_command", scopes=[Scope.ISSUE])(self.TestCommand)

            # Verify the decorator called register with correct parameters
            mock_registry.register.assert_called_once_with(self.TestCommand, "test_command", [Scope.ISSUE])

            # Verify the class is returned unchanged
            assert self.TestCommand.__name__ == "TestCommand"

    def test_decorator_with_multiple_scopes(self):
        """Test that decorator works with multiple scopes."""
        with patch("slash_commands.decorator.slash_command_registry") as mock_registry:
            scopes = [Scope.ISSUE, Scope.MERGE_REQUEST]

            slash_command(command="multi_scope_command", scopes=scopes)(self.TestCommand)

            mock_registry.register.assert_called_once_with(self.TestCommand, "multi_scope_command", scopes)

    def test_decorator_can_be_applied_to_multiple_classes(self):
        """Test that decorator can be applied to multiple different classes."""
        with patch("slash_commands.decorator.slash_command_registry") as mock_registry:
            slash_command(command="command1", scopes=[Scope.ISSUE])(self.TestCommand)

            @slash_command(command="command2", scopes=[Scope.MERGE_REQUEST])
            class Command2(self.TestCommand):
                pass

            # Verify both registrations occurred
            assert mock_registry.register.call_count == 2

            # Check the specific calls
            calls = mock_registry.register.call_args_list
            assert calls[0][0] == (self.TestCommand, "command1", [Scope.ISSUE])
            assert calls[1][0] == (Command2, "command2", [Scope.MERGE_REQUEST])

    def test_decorator_with_inheritance(self):
        """Test that decorator works with class inheritance."""
        with patch("slash_commands.decorator.slash_command_registry") as mock_registry:

            class BaseCommand(self.TestCommand):
                def shared_method(self):
                    return "shared"

            @slash_command(command="inherited_command", scopes=[Scope.ISSUE])
            class InheritedCommand(BaseCommand):
                pass

            mock_registry.register.assert_called_once_with(InheritedCommand, "inherited_command", [Scope.ISSUE])

            # Verify inheritance still works
            command = InheritedCommand(scope=Scope.ISSUE, repo_id="repo1", bot_username="bot")
            assert command.shared_method() == "shared"
