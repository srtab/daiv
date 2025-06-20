from abc import ABC, abstractmethod
from enum import StrEnum


class Scope(StrEnum):
    ISSUE = "issue"
    MERGE_REQUEST = "merge_request"


class QuickAction(ABC):
    """
    Base class for quick actions.
    """

    verb: str
    scopes: list[Scope]

    @property
    @abstractmethod
    def description(self) -> str:
        """
        Get the description of the quick action.

        Returns:
            str: The description of the quick action.
        """
        pass

    @abstractmethod
    def execute(
        self,
        repo_id: str,
        scope: Scope,
        note: dict,
        user: dict,
        issue: dict | None = None,
        merge_request: dict | None = None,
        args: list[str] | None = None,
    ) -> None:
        """
        Execute the quick action.

        Args:
            repo_id: The repository ID.
            scope: The scope of the quick action.
            note: The note data that triggered the action.
            user: The user who triggered the action.
            issue: The issue data (if applicable).
            merge_request: The merge request data (if applicable).
            args: Additional parameters from the command.
        """
        pass
