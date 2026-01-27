from django.template.loader import render_to_string

from codebase.base import Scope
from core.constants import BOT_NAME
from slash_commands.base import SlashCommand
from slash_commands.decorator import slash_command
from slash_commands.registry import slash_command_registry


@slash_command(command="help", scopes=[Scope.GLOBAL, Scope.ISSUE, Scope.MERGE_REQUEST])
class HelpSlashCommand(SlashCommand):
    """
    Shows the help message for the available slash commands.
    """

    description: str = "Shows the help message with the available slash commands."

    async def execute_for_agent(
        self,
        *,
        args: str,
        scope: Scope,
        repo_id: str,
        bot_username: str,
        issue_iid: int | None = None,
        merge_request_id: int | None = None,
    ) -> str:
        """
        Execute help command for agent middleware.

        Args:
            args: Additional parameters from the command.
            scope: The scope to get the help message for.
            repo_id: The repository ID.
            bot_username: The bot username.
            issue_iid: The issue IID (for Issue scope).
            merge_request_id: The merge request ID (for Merge Request scope).

        Returns:
            The help message for the given scope.
        """
        commands_help = [command().help() for command in slash_command_registry.get_commands(scope=scope)]
        if not commands_help:
            return "No slash commands available."
        return render_to_string(
            "slash_commands/slash_commands_help.txt",
            {"bot_name": BOT_NAME, "scope": scope.value.lower(), "actions": commands_help},
        )
