from pathlib import Path
from typing import TYPE_CHECKING

from langchain_core.runnables import RunnableConfig
from langgraph.store.memory import InMemoryStore

from automation.agent.pr_describer.agent import PullRequestDescriberAgent
from automation.agent.pr_describer.conf import settings as pr_describer_settings
from automation.agent.utils import get_context_file_content
from codebase.clients import RepoClient
from codebase.utils import GitManager, redact_diff_content

if TYPE_CHECKING:
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
        self.store = InMemoryStore()
        self.git_manager = GitManager(self.ctx.repo)

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

    async def _commit_changes(self, *, thread_id: str | None = None, skip_ci: bool = False):
        """
        Commit changes to the branch.

        Args:
            thread_id: The thread ID.
            skip_ci: Whether to skip the CI.
        """
        pr_describer = await PullRequestDescriberAgent.get_runnable(model=self.ctx.config.models.pr_describer.model)
        changes_description = await pr_describer.ainvoke(
            {
                "diff": redact_diff_content(self.git_manager.get_diff(), self.ctx.config.omit_content_patterns),
                "context_file_content": await get_context_file_content(
                    Path(self.ctx.repo.working_dir), self.ctx.config.context_file_name
                ),
            },
            config=RunnableConfig(
                tags=[pr_describer_settings.NAME, str(self.client.git_platform)], configurable={"thread_id": thread_id}
            ),
        )

        self.git_manager.commit_and_push_changes(
            changes_description.commit_message, branch_name=self.ctx.repo.active_branch.name, skip_ci=skip_ci
        )
