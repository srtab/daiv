from typing import TYPE_CHECKING, cast

from langchain_core.runnables import RunnableConfig
from langgraph.store.memory import InMemoryStore

from automation.agents.pr_describer.agent import PullRequestDescriberAgent
from automation.agents.pr_describer.conf import settings as pr_describer_settings
from codebase.repo_config import RepositoryConfig

if TYPE_CHECKING:
    from codebase.base import FileChange
    from codebase.clients import RepoClient


class BaseManager:
    """
    Base class for all managers.
    """

    _comment_id: str | None = None
    """ The comment ID where DAIV comments are stored. """

    def __init__(self, client: RepoClient, repo_id: str, ref: str | None = None):
        self.client = client
        self.repo_id = repo_id
        self.repo_config = RepositoryConfig.get_config(repo_id)
        self._file_changes_store = InMemoryStore()
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
            raise ValueError(
                f"Failed to get a unique branch name for {original_branch_name}, max attempts reached {max_attempts}."
            )

        return branch_name

    async def _commit_changes(
        self, *, file_changes: list[FileChange], thread_id: str | None = None, skip_ci: bool = False
    ):
        """
        Commit changes to the merge request.

        Args:
            file_changes: The file changes to commit.
            thread_id: The thread ID.
            skip_ci: Whether to skip the CI.
        """
        pr_describer = await PullRequestDescriberAgent.get_runnable()
        changes_description = await pr_describer.ainvoke(
            {"changes": file_changes, "branch_name_convention": self.repo_config.pull_request.branch_name_convention},
            config=RunnableConfig(
                tags=[pr_describer_settings.NAME, str(self.client.client_slug)], configurable={"thread_id": thread_id}
            ),
        )
        commit_message = changes_description.commit_message
        if skip_ci:
            commit_message = f"[skip ci] {commit_message}"

        self.client.commit_changes(self.repo_id, self.ref, commit_message, file_changes)

    def _create_or_update_comment(self, note_message: str):
        """
        Create or update a comment on the issue.
        """
        if self._comment_id is not None:
            self.client.update_issue_comment(self.repo_id, self.issue.iid, self._comment_id, note_message)
        else:
            self._comment_id = self.client.create_issue_comment(self.repo_id, self.issue.iid, note_message)
