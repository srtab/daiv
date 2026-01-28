from unittest.mock import MagicMock, Mock, patch

from pytest import fixture

from codebase.base import Scope
from slash_commands.actions.help import HelpSlashCommand


@fixture
def help_slash_command() -> HelpSlashCommand:
    """Set up test fixtures."""
    command = HelpSlashCommand(scope=Scope.ISSUE, repo_id="repo1", bot_username="bot")
    command.client = MagicMock(current_user=MagicMock(username="bot"))
    return command


def test_help_command_has_correct_attributes():
    """Test that HelpSlashCommand has the expected attributes set by decorator."""
    assert hasattr(HelpSlashCommand, "command")
    assert hasattr(HelpSlashCommand, "scopes")
    assert HelpSlashCommand.command == "help"
    assert Scope.ISSUE in HelpSlashCommand.scopes
    assert Scope.MERGE_REQUEST in HelpSlashCommand.scopes


@patch("slash_commands.actions.help.slash_command_registry.get_commands", new=Mock(return_value=[]))
async def test_help_command_returns_correct_message(help_slash_command: HelpSlashCommand):
    """Test that HelpSlashCommand returns the correct message."""
    message = await help_slash_command.execute_for_agent(args="", available_skills=[])
    assert message == "No slash commands available."
