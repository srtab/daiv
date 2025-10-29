from typing import TYPE_CHECKING

from langchain_core.runnables import RunnableConfig
from langgraph.store.memory import InMemoryStore

from automation.agents.pr_describer.agent import PullRequestDescriberAgent
from automation.agents.pr_describer.conf import settings as pr_describer_settings
from codebase.clients import RepoClient

if TYPE_CHECKING:
    from codebase.base import FileChange
    from codebase.context import RuntimeCtx


class BaseManager:
    """
    Base class for all managers.
    """

    _comment_id: str | None = None
    """ The comment ID where DAIV comments are stored. """

    def __init__(self, *, runtime_ctx: RuntimeCtx):
        self.ctx = runtime_ctx
        self.client = RepoClient.create_instance()
        self._file_changes_store = InMemoryStore()

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

        while self.client.repository_branch_exists(self.ctx.repo_id, branch_name) and suffix_count < max_attempts:
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
            {"changes": file_changes, "branch_name_convention": self.ctx.config.pull_request.branch_name_convention},
            config=RunnableConfig(
                tags=[pr_describer_settings.NAME, str(self.client.client_slug)], configurable={"thread_id": thread_id}
            ),
        )
        commit_message = changes_description.commit_message
        if skip_ci:
            commit_message = f"[skip ci] {commit_message}"

        self.client.commit_changes(self.ctx.repo_id, self.ctx.ref, commit_message, file_changes)
