from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import TYPE_CHECKING

from langchain_core.prompts import jinja2_formatter

from automation.quick_actions.templates import UNKNOWN_QUICK_ACTION_TEMPLATE
from codebase.clients import RepoClient

if TYPE_CHECKING:
    from codebase.base import Discussion, Issue, MergeRequest, Note


class TriggerLocation(StrEnum):
    DISCUSSION = "discussion"
    REPLY = "reply"
    BOTH = "both"


class BaseAction:
    """
    Base class for actions.
    """

    trigger: str
    description: str
    location: TriggerLocation = TriggerLocation.BOTH

    @classmethod
    def match(cls, action: str, is_reply: bool = False) -> bool:
        """
        Check if the action matches the trigger.
        """
        return action.lower() == cls.trigger.lower() and cls.match_location(is_reply)

    @classmethod
    def match_location(cls, is_reply: bool) -> bool:
        """
        Check if the action matches the location.
        """
        return (
            (cls.location == TriggerLocation.REPLY and is_reply)
            or (cls.location == TriggerLocation.DISCUSSION and not is_reply)
            or (cls.location == TriggerLocation.BOTH)
        )

    @classmethod
    def help(cls, verb: str, bot_username: str) -> str:
        """
        Get the help message for the action.
        """
        return f" * `@{bot_username} {verb} {cls.trigger}` - {cls.description}"


class Scope(StrEnum):
    ISSUE = "Issue"
    MERGE_REQUEST = "Merge Request"


class QuickAction(ABC):
    """
    Base class for quick actions.
    """

    verb: str
    scopes: list[Scope]
    actions: list[BaseAction]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = RepoClient.create_instance()

    @classmethod
    def help(cls, bot_username: str, is_reply: bool = False) -> str:
        """
        Get the help message for the quick action.
        """
        return "\n".join([
            action.help(cls.verb, bot_username) for action in cls.actions if action.match_location(is_reply)
        ])

    async def execute(
        self,
        repo_id: str,
        *,
        args: str,
        scope: Scope,
        discussion: Discussion,
        note: Note,
        issue: Issue | None = None,
        merge_request: MergeRequest | None = None,
    ) -> None:
        """
        Execute the quick action.

        Args:
            repo_id: The repository ID.
            args: The arguments from the command.
            scope: The scope of the quick action.
            discussion: The discussion that triggered the action.
            note: The note that triggered the action.
            issue: The issue where the action was triggered (if applicable).
            merge_request: The merge request where the action was triggered (if applicable).
        """
        is_reply = len(discussion.notes) > 1

        if not self.validate_action(args, is_reply):
            self._add_invalid_action_message(
                repo_id,
                merge_request.merge_request_id if merge_request else issue.iid,
                discussion.id,
                args,
                is_reply=is_reply,
                scope=scope,
            )
            return

        return await self.execute_action(
            repo_id,
            args=args,
            scope=scope,
            discussion=discussion,
            note=note,
            issue=issue,
            merge_request=merge_request,
            is_reply=is_reply,
        )

    @abstractmethod
    async def execute_action(
        self,
        repo_id: str,
        *,
        args: str,
        scope: Scope,
        discussion: Discussion,
        note: Note,
        issue: Issue | None = None,
        merge_request: MergeRequest | None = None,
        is_reply: bool = False,
    ) -> None:
        """
        Use this method to implement the specific logic for the action to be executed.

        Args:
            repo_id: The repository ID.
            scope: The scope of the quick action.
            discussion: The discussion that triggered the action.
            note: The note that triggered the action.
            issue: The issue where the action was triggered (if applicable).
            merge_request: The merge request where the action was triggered (if applicable).
            args: Additional parameters from the command.
            is_reply: Whether the action was triggered as a reply.
        """

    def validate_action(self, action: str, is_reply: bool) -> bool:
        """
        Validate the action is valid.

        Args:
            action: The action to validate.
            is_reply: Whether the action was triggered as a reply.

        Returns:
            bool: True if the action is valid, False otherwise.
        """
        return any(action_item.match(action, is_reply) for action_item in self.actions)

    def _add_invalid_action_message(
        self,
        repo_id: str,
        object_id: int,
        note_discussion_id: str,
        invalid_action: str,
        scope: Scope,
        is_reply: bool = False,
    ) -> None:
        """
        Add an invalid action message to the merge request discussion.

        Args:
            repo_id: The repository ID.
            object_id: The merge request or issue ID.
            note_discussion_id: The discussion ID of the note.
            invalid_action: The invalid action.
            scope: The scope of the quick action.
            is_reply: Whether the action was triggered as a reply.
        """
        note_message = jinja2_formatter(
            UNKNOWN_QUICK_ACTION_TEMPLATE,
            bot_name=self.client.current_user.username,
            verb=self.verb,
            help=self.help(self.client.current_user.username, is_reply),
            invalid_action=invalid_action,
        )

        if scope == Scope.MERGE_REQUEST:
            self.client.create_merge_request_discussion_note(
                repo_id, object_id, note_message, note_discussion_id, mark_as_resolved=True
            )
        elif scope == Scope.ISSUE:
            self.client.create_issue_discussion_note(repo_id, object_id, note_message, note_discussion_id)
