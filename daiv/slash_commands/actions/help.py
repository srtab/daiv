from typing import TYPE_CHECKING

from django.template.loader import render_to_string

from codebase.base import Scope
from core.constants import BOT_NAME
from slash_commands.base import SlashCommand
from slash_commands.decorator import slash_command
from slash_commands.registry import slash_command_registry

if TYPE_CHECKING:
    from deepagents.middleware.skills import SkillMetadata


@slash_command(command="help", scopes=[Scope.GLOBAL, Scope.ISSUE, Scope.MERGE_REQUEST])
class HelpSlashCommand(SlashCommand):
    """
    Shows the help message for the available slash commands.
    """

    description: str = "Shows the help message with the available slash commands."

    async def execute_for_agent(self, *, args: str, available_skills: list[SkillMetadata], **kwargs) -> str:
        """
        Execute help command for agent middleware.

        Args:
            args: Additional parameters from the command.
            available_skills: The list of available skills.

        Returns:
            The help message for the given scope.
        """
        commands_help = [
            command(scope=self.scope, repo_id=self.repo_id, bot_username=self.bot_username).help()
            for command in slash_command_registry.get_commands(scope=self.scope)
        ]

        commands_help += [self._format_skill_help(skill) for skill in available_skills]

        if not commands_help:
            return "No slash commands available."

        return render_to_string(
            "slash_commands/slash_commands_help.txt",
            {
                "bot_name": BOT_NAME,
                "need_mention_to_invoke": self.need_mention,
                "bot_username": self.bot_username,
                "actions": commands_help,
            },
        )

    def _format_skill_help(self, skill: SkillMetadata) -> str:
        """
        Format the help message for a skill.

        Args:
            skill: The skill metadata.

        Returns:
            The help message for the skill.
        """
        return f"| `/{skill['name']}` | {skill['description']} |"
