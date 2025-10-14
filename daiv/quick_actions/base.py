from __future__ import annotations

from abc import ABC
from enum import StrEnum
from typing import TYPE_CHECKING

from langchain_core.prompts import jinja2_formatter

from codebase.clients import RepoClient
from quick_actions.templates import INVALID_ARGS_QUICK_ACTION_TEMPLATE

if TYPE_CHECKING:
    from codebase.base import Discussion, Issue, MergeRequest


class Scope(StrEnum):
    ISSUE = "Issue"
    MERGE_REQUEST = "Merge Request"


class QuickAction(ABC):
    """
    Base class for quick actions.
    """

    command: str
    scopes: list[Scope]
    description: str

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = RepoClient.create_instance()

    def help(self) -> str:
        """
        Get the help message for the action.
        """
        return f" * `{self.command_to_activate}` - {self.description}"

    @property
    def command_to_activate(self) -> str:
        """
        Get the command to activate the action.
        """
        return f"@{self.client.current_user.username} /{self.command}"

    async def execute_for_issue(self, repo_id: str, *, args: str, comment: Discussion, issue: Issue) -> None:
        """
        Execute the quick action for an issue.
        """
        if not self.validate_arguments(args):
            self._add_invalid_args_message(repo_id, issue.iid, comment.id, args, scope=Scope.ISSUE)
            return

        return await self.execute_action_for_issue(repo_id, args=args, comment=comment, issue=issue)

    async def execute_for_merge_request(
        self, repo_id: str, *, args: str, comment: Discussion, merge_request: MergeRequest
    ) -> None:
        """
        Execute the quick action for a merge request.
        """
        if not self.validate_arguments(args):
            self._add_invalid_args_message(
                repo_id, merge_request.merge_request_id, comment.id, args, scope=Scope.MERGE_REQUEST
            )
            return

        return await self.execute_action_for_merge_request(
            repo_id, args=args, comment=comment, merge_request=merge_request
        )

    async def execute_action_for_issue(self, repo_id: str, *, args: str, comment: Discussion, issue: Issue) -> None:
        """
        Use this method to implement the specific logic for the action to be executed.

        Args:
            repo_id: The repository ID.
            comment: The comment that triggered the action.
            issue: The issue where the action was triggered (if applicable).
            args: Additional parameters from the command.
        """
        raise NotImplementedError("execute_action_for_issue is not implemented")

    async def execute_action_for_merge_request(
        self, repo_id: str, *, args: str, comment: Discussion, merge_request: MergeRequest
    ) -> None:
        """
        Execute the quick action for a merge request.

        Args:
            repo_id: The repository ID.
            comment: The comment that triggered the action.
            merge_request: The merge request where the action was triggered (if applicable).
            args: Additional parameters from the command.
        """
        raise NotImplementedError("execute_action_for_merge_request is not implemented")

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
            scope: The scope of the quick action.
        """
        note_message = jinja2_formatter(
            INVALID_ARGS_QUICK_ACTION_TEMPLATE,
            bot_name=self.client.current_user.username,
            command=self.command,
            help=self.help(),
            invalid_args=invalid_args,
        )

        if scope == Scope.MERGE_REQUEST:
            self.client.create_merge_request_comment(
                repo_id, object_id, note_message, reply_to_id=comment_id, mark_as_resolved=True
            )
        elif scope == Scope.ISSUE:
            self.client.create_issue_comment(repo_id, object_id, note_message, reply_to_id=comment_id)
