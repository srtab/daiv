from unittest.mock import MagicMock, Mock, patch

from django.test import override_settings

from pytest import fixture

from codebase.base import Scope
from slash_commands.actions.clear import ClearSlashCommand


@fixture
def clear_slash_command_issue() -> ClearSlashCommand:
    """Set up test fixtures for issue scope."""
    command = ClearSlashCommand(scope=Scope.ISSUE, repo_id="repo1", bot_username="bot")
    command.client = MagicMock(current_user=MagicMock(username="bot"))
    return command


@fixture
def clear_slash_command_mr() -> ClearSlashCommand:
    """Set up test fixtures for merge request scope."""
    command = ClearSlashCommand(scope=Scope.MERGE_REQUEST, repo_id="repo1", bot_username="bot")
    command.client = MagicMock(current_user=MagicMock(username="bot"))
    return command


def test_clear_command_has_correct_attributes():
    """Test that ClearSlashCommand has the expected attributes set by decorator."""
    assert hasattr(ClearSlashCommand, "command")
    assert hasattr(ClearSlashCommand, "scopes")
    assert ClearSlashCommand.command == "clear"
    assert Scope.ISSUE in ClearSlashCommand.scopes
    assert Scope.MERGE_REQUEST in ClearSlashCommand.scopes
    assert Scope.GLOBAL not in ClearSlashCommand.scopes


@override_settings(DB_URI="postgresql://test:test@localhost:5432/test")
@patch("slash_commands.actions.clear.PostgresSaver")
async def test_clear_command_for_issue(mock_postgres_saver: Mock, clear_slash_command_issue: ClearSlashCommand):
    """Test that ClearSlashCommand deletes the thread for an issue."""
    mock_checkpointer = MagicMock()
    mock_postgres_saver.from_conn_string.return_value.__enter__.return_value = mock_checkpointer

    message = await clear_slash_command_issue.execute_for_agent(args="", issue_iid=42)

    # Verify the thread was deleted
    mock_checkpointer.delete_thread.assert_called_once()
    assert "cleared successfully" in message
    assert "✅" in message


@override_settings(DB_URI="postgresql://test:test@localhost:5432/test")
@patch("slash_commands.actions.clear.PostgresSaver")
async def test_clear_command_for_merge_request(
    mock_postgres_saver: Mock, clear_slash_command_mr: ClearSlashCommand
):
    """Test that ClearSlashCommand deletes the thread for a merge request."""
    mock_checkpointer = MagicMock()
    mock_postgres_saver.from_conn_string.return_value.__enter__.return_value = mock_checkpointer

    message = await clear_slash_command_mr.execute_for_agent(args="", merge_request_id=123)

    # Verify the thread was deleted
    mock_checkpointer.delete_thread.assert_called_once()
    assert "cleared successfully" in message
    assert "✅" in message


@patch("slash_commands.actions.clear.PostgresSaver")
async def test_clear_command_without_issue_iid(
    mock_postgres_saver: Mock, clear_slash_command_issue: ClearSlashCommand
):
    """Test that ClearSlashCommand returns an error when issue_iid is missing."""
    message = await clear_slash_command_issue.execute_for_agent(args="")

    # Verify no thread deletion attempted
    mock_postgres_saver.from_conn_string.assert_not_called()
    assert "only available for issues and merge requests" in message


@override_settings(DB_URI="postgresql://test:test@localhost:5432/test")
@patch("slash_commands.actions.clear.PostgresSaver")
async def test_clear_command_handles_exceptions(
    mock_postgres_saver: Mock, clear_slash_command_issue: ClearSlashCommand
):
    """Test that ClearSlashCommand handles exceptions gracefully."""
    mock_checkpointer = MagicMock()
    mock_checkpointer.delete_thread.side_effect = Exception("Database error")
    mock_postgres_saver.from_conn_string.return_value.__enter__.return_value = mock_checkpointer

    message = await clear_slash_command_issue.execute_for_agent(args="", issue_iid=42)

    assert "Failed to clear" in message
    assert "❌" in message
