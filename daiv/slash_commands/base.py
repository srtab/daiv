from __future__ import annotations

from abc import ABC

from codebase.base import Scope


class SlashCommand(ABC):
    """
    Base class for slash commands.
    """

    command: str
    scopes: list[Scope]
    description: str

    def __init__(self, *, scope: Scope, repo_id: str, bot_username: str | None = None):
        self.scope = scope
        self.repo_id = repo_id
        self.bot_username = bot_username

    @property
    def command_to_invoke(self) -> str:
        """
        Get the command to activate the action.
        """
        return f"/{self.command}"

    @property
    def need_mention(self) -> bool:
        """
        Check if the command needs to be mentioned.
        """
        return self.scope != Scope.GLOBAL and self.bot_username

    def help(self) -> str:
        """
        Get the help message for the command.
        """
        return f" * `{self.command_to_invoke}` - {self.description}"

    async def execute_for_agent(
        self, *, args: str, issue_iid: int | None = None, merge_request_id: int | None = None
    ) -> str:
        """
        Execute the slash command for agent middleware.

        This method should be implemented by subclasses to return a result content
        instead of side-effecting via client calls.

        Args:
            args: Additional parameters from the command.
            issue_iid: The issue IID (for Issue scope).
            merge_request_id: The merge request ID (for Merge Request scope).

        Returns:
            The result content.
        """
        raise NotImplementedError("execute_for_agent is not implemented")
