from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import TYPE_CHECKING

from langchain_core.prompts.string import jinja2_formatter

from automation.quick_actions.templates import UNKNOWN_QUICK_ACTION_TEMPLATE

if TYPE_CHECKING:
    from codebase.base import Discussion, Issue, MergeRequest, Note


class BaseAction(StrEnum):
    """
    Base class for actions.
    """

    @staticmethod
    def get_name(action: BaseAction) -> str:
        """
        Get the name of the action.
        """
        return action.name.lower().replace("_", " ")

    @staticmethod
    def split_name(action: BaseAction) -> list[str]:
        """
        Get the arguments of the action.
        """
        return action.name.lower().split("_")

    @staticmethod
    def is_reply(action: BaseAction) -> bool:
        """
        Check if the action is specific to a reply.
        """
        return False


class Scope(StrEnum):
    ISSUE = "Issue"
    MERGE_REQUEST = "Merge Request"


class QuickAction(ABC):
    """
    Base class for quick actions.
    """

    verb: str
    scopes: list[Scope]
    can_reply: bool = False

    @staticmethod
    @abstractmethod
    def description() -> str:
        """
        Get the description of the quick action.

        Returns:
            str: The description of the quick action.
        """
        pass

    @abstractmethod
    async def execute(
        self,
        repo_id: str,
        *,
        scope: Scope,
        discussion: Discussion,
        note: Note,
        issue: Issue | None = None,
        merge_request: MergeRequest | None = None,
        args: str | None = None,
    ) -> None:
        """
        Execute the quick action.

        Args:
            repo_id: The repository ID.
            scope: The scope of the quick action.
            discussion: The discussion that triggered the action.
            note: The note that triggered the action.
            issue: The issue where the action was triggered (if applicable).
            merge_request: The merge request where the action was triggered (if applicable).
            args: Additional parameters from the command.
        """
        pass

    @classmethod
    def help(cls, username: str, is_reply: bool = False) -> str:
        """
        Get the help message for the quick action.

        Args:
            username: The username of the bot.
            is_reply: Whether the action was triggered as a reply.
        """
        return f"* `@{username} {cls.verb}` - {cls.description()}"

    def _invalid_action_message(self, username: str, invalid_action: str | None, is_reply: bool = False) -> str:
        """
        Get the invalid action message for the quick action.

        Args:
            username: The username of the bot.
            invalid_action: The invalid action that was triggered.
            is_reply: Whether the action was triggered as a reply.
        """
        return jinja2_formatter(
            UNKNOWN_QUICK_ACTION_TEMPLATE,
            bot_name=username,
            verb=self.verb,
            help=self.help(username, is_reply),
            invalid_action=invalid_action,
        )
