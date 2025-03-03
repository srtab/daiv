from typing import cast

from langgraph.store.memory import InMemoryStore

from automation.agents.pr_describer.agent import PullRequestDescriberAgent
from automation.utils import file_changes_namespace
from codebase.base import FileChange
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
        self._file_changes_store = InMemoryStore()
        self.ref = cast("str", ref or self.repo_config.default_branch)

    def _get_file_changes(self, *, store: InMemoryStore | None = None) -> list[FileChange]:
        """
        Get the file changes from the store.
        """
        return [
            cast("FileChange", item.value["data"])
            for item in (store or self._file_changes_store).search(file_changes_namespace(self.repo_id, self.ref))
        ]

    def _set_file_changes(self, file_changes: list[FileChange], *, store: InMemoryStore | None = None):
        """
        Set the file changes in the store.
        """
        for file_change in file_changes:
            (store or self._file_changes_store).put(
                file_changes_namespace(self.repo_id, self.ref),
                file_change.file_path,
                {"data": file_change, "action": file_change.action},
            )

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
            raise ValueError(
                f"Failed to get a unique branch name for {original_branch_name}, max attempts reached {max_attempts}."
            )

        return branch_name

    def _commit_changes(self, *, file_changes: list[FileChange], thread_id: str | None = None, skip_ci: bool = False):
        """
        Commit changes to the merge request.

        Args:
            file_changes: The file changes to commit.
            thread_id: The thread ID.
            skip_ci: Whether to skip the CI.
        """
        pr_describer = PullRequestDescriberAgent()
        changes_description = pr_describer.agent.invoke(
            {"changes": file_changes, "branch_name_convention": self.repo_config.branch_name_convention},
            config={
                "run_name": "PullRequestDescriber",
                "tags": ["pull_request_describer", str(self.client.client_slug)],
                "configurable": {"thread_id": thread_id},
            },
        )
        commit_message = changes_description.commit_message
        if skip_ci:
            commit_message = f"[skip ci] {commit_message}"

        self.client.commit_changes(self.repo_id, self.ref, commit_message, file_changes)
