from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import TYPE_CHECKING

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


class Scope(StrEnum):
    ISSUE = "issue"
    MERGE_REQUEST = "merge_request"


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
    def help(cls, username: str) -> str:
        """
        Get the help message for the quick action.
        """
        return f"* `@{username} {cls.verb}` - {cls.description()}"
