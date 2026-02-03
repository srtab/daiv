from typing import TYPE_CHECKING

from django.template.loader import render_to_string

from codebase.base import Scope
from slash_commands.base import SlashCommand
from slash_commands.decorator import slash_command

if TYPE_CHECKING:
    from deepagents.graph import SubAgent


@slash_command(command="agents", scopes=[Scope.GLOBAL, Scope.ISSUE, Scope.MERGE_REQUEST])
class AgentsSlashCommand(SlashCommand):
    """
    Shows the list of available sub-agents.
    """

    description: str = "Shows the list of available sub-agents with their names and descriptions."

    async def execute_for_agent(self, *, args: str, available_subagents: list[SubAgent], **kwargs) -> str:
        """
        Execute agents command for agent middleware.

        Args:
            args: Additional parameters from the command.
            available_subagents: The list of available sub-agents.

        Returns:
            The list of available sub-agents.
        """
        if not available_subagents:
            return "No sub-agents available."

        agents_list = [
            {"name": subagent["name"], "description": subagent["description"]} for subagent in available_subagents
        ]

        return render_to_string("slash_commands/agents_list.txt", {"agents": agents_list})
