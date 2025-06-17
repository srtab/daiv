from abc import ABC, abstractmethod


class QuickAction(ABC):
    """
    Base class for quick actions.
    """

    identifier: str
    supports_issues: bool = True
    supports_merge_requests: bool = True

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
        note: dict,
        user: dict,
        issue: dict | None = None,
        merge_request: dict | None = None,
        params: str | None = None,
    ) -> str:
        """
        Execute the quick action.

        Args:
            repo_id: The repository ID.
            note: The note data that triggered the action.
            user: The user who triggered the action.
            issue: The issue data (if applicable).
            merge_request: The merge request data (if applicable).
            params: Additional parameters from the command.

        Returns:
            str: The result message to post as a comment.
        """
        pass