from typing import TYPE_CHECKING
from unittest.mock import MagicMock

from pytest import fixture

from codebase.base import Scope
from slash_commands.actions.agents import AgentsSlashCommand

if TYPE_CHECKING:
    from deepagents.graph import SubAgent


@fixture
def agents_slash_command() -> AgentsSlashCommand:
    """Set up test fixtures."""
    command = AgentsSlashCommand(scope=Scope.ISSUE, repo_id="repo1", bot_username="bot")
    command.client = MagicMock(current_user=MagicMock(username="bot"))
    return command


@fixture
def mock_subagents() -> list[SubAgent]:
    """Create mock subagents for testing."""
    return [
        {
            "name": "general-purpose",
            "description": "General-purpose agent for researching and executing tasks.",
            "system_prompt": "Test prompt",
            "tools": [],
        },
        {
            "name": "explore",
            "description": "Fast agent specialized for exploring codebases.",
            "system_prompt": "Test prompt",
            "tools": [],
        },
        {
            "name": "docs-research",
            "description": "Agent for fetching up-to-date documentation.",
            "system_prompt": "Test prompt",
            "tools": [],
        },
    ]


def test_agents_command_has_correct_attributes():
    """Test that AgentsSlashCommand has the expected attributes set by decorator."""
    assert hasattr(AgentsSlashCommand, "command")
    assert hasattr(AgentsSlashCommand, "scopes")
    assert AgentsSlashCommand.command == "agents"
    assert Scope.GLOBAL in AgentsSlashCommand.scopes
    assert Scope.ISSUE in AgentsSlashCommand.scopes
    assert Scope.MERGE_REQUEST in AgentsSlashCommand.scopes


async def test_agents_command_with_no_subagents(agents_slash_command: AgentsSlashCommand):
    """Test that AgentsSlashCommand returns the correct message when no subagents are available."""
    message = await agents_slash_command.execute_for_agent(args="", available_subagents=[])
    assert message == "No sub-agents available."


async def test_agents_command_with_subagents(agents_slash_command: AgentsSlashCommand, mock_subagents: list[SubAgent]):
    """Test that AgentsSlashCommand returns the correct formatted message with subagents."""
    message = await agents_slash_command.execute_for_agent(args="", available_subagents=mock_subagents)

    # Check that the message contains the expected content
    assert "Available Sub-Agents" in message
    assert "general-purpose" in message
    assert "explore" in message
    assert "docs-research" in message
    assert "General-purpose agent for researching and executing tasks." in message
    assert "Fast agent specialized for exploring codebases." in message
    assert "Agent for fetching up-to-date documentation." in message
