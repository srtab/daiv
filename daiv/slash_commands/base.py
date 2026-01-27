from __future__ import annotations

from abc import ABC

from django.template.loader import render_to_string

from codebase.base import Scope
from codebase.clients import RepoClient


class SlashCommand(ABC):
    """
    Base class for slash commands.
    """

    command: str
    scopes: list[Scope]
    description: str

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = RepoClient.create_instance()

    def help(self) -> str:
        """
        Get the help message for the command.
        """
        return f" * `{self.command_to_activate}` - {self.description}"

    @property
    def command_to_activate(self) -> str:
        """
        Get the command to activate the action.
        """
        return f"@{self.client.current_user.username} /{self.command}"

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
        Execute the slash command for agent middleware.

        This method should be implemented by subclasses to return a result content
        instead of side-effecting via client calls.

        Args:
            args: Additional parameters from the command.
            scope: The scope (Issue or Merge Request).
            repo_id: The repository ID.
            bot_username: The bot username.
            issue_iid: The issue IID (for Issue scope).
            merge_request_id: The merge request ID (for Merge Request scope).

        Returns:
            The result content.
        """
        raise NotImplementedError("execute_for_agent is not implemented")

    def validate_arguments(self, args: str) -> bool:
        """
        Validate the arguments are valid.

        Args:
            args: The arguments to validate.

        Returns:
            bool: True if the arguments are valid, False otherwise.
        """
        return True

    def _add_invalid_args_message(
        self, repo_id: str, object_id: int, comment_id: str, invalid_args: str, scope: Scope
    ) -> None:
        """
        Add an invalid arguments message to the merge request discussion.

        Args:
            repo_id: The repository ID.
            object_id: The merge request or issue ID.
            comment_id: The comment ID of the note.
            invalid_args: The invalid arguments.
            scope: The scope of the slash command.
        """
        note_message = render_to_string(
            "slash_commands/invalid_args.txt",
            {
                "bot_name": self.client.current_user.username,
                "command": self.command,
                "help": self.help(),
                "invalid_args": invalid_args,
            },
        )

        if scope == Scope.MERGE_REQUEST:
            self.client.create_merge_request_comment(
                repo_id, object_id, note_message, reply_to_id=comment_id, mark_as_resolved=True
            )
        elif scope == Scope.ISSUE:
            self.client.create_issue_comment(repo_id, object_id, note_message, reply_to_id=comment_id)
