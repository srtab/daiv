from typing import cast

from codebase.clients import AllRepoClient
from core.config import RepositoryConfig


class BaseManager:
    """
    Base class for all managers.
    """

    def __init__(self, client: AllRepoClient, repo_id: str, ref: str | None = None):
        self.client = client
        self.repo_id = repo_id
        self.repo_config = RepositoryConfig.get_config(repo_id)
        self.ref = cast("str", ref or self.repo_config.default_branch)

    def _get_unique_branch_name(self, original_branch_name: str, max_attempts: int = 10) -> str:
        """
        Get a unique branch name.

        Args:
            original_branch_name: The original branch name.
            max_attempts: The maximum number of attempts to get a unique branch name.

        Returns:
            A unique branch name.

        Raises:
            ValueError: If the maximum number of attempts is reached.
        """
        suffix_count = 1
        branch_name = original_branch_name

        while self.client.repository_branch_exists(self.repo_id, branch_name) and suffix_count < max_attempts:
            branch_name = f"{original_branch_name}-{suffix_count}"
            suffix_count += 1

        if suffix_count == max_attempts:
            raise ValueError(f"Failed to get a unique branch name for {original_branch_name}")

        return branch_name
