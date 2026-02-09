from __future__ import annotations

import logging
from abc import abstractmethod
from textwrap import dedent
from typing import TYPE_CHECKING, Any, cast

from django.template.loader import render_to_string

from codebase.base import GitPlatform, MergeRequest, Scope
from codebase.clients import RepoClient
from codebase.utils import GitManager, redact_diff_content
from core.constants import BOT_LABEL, BOT_NAME

from .pr_describer.graph import create_pr_describer_agent

if TYPE_CHECKING:
    from codebase.context import RuntimeCtx

    from .pr_describer.schemas import PullRequestMetadata

logger = logging.getLogger("daiv.tools")


class ChangePublisher:
    """
    Publisher for changes made by the agent.
    """

    def __init__(self, ctx: RuntimeCtx):
        """
        Initialize the publisher.
        """
        self.ctx = ctx

    @abstractmethod
    async def publish(self, **kwargs) -> Any:
        """
        Publish the changes.
        """


class GitChangePublisher(ChangePublisher):
    """
    Publisher for changes made by the agent to the Git repository.
    """

    async def publish(
        self,
        *,
        branch_name: str | None = None,
        merge_request_id: int | None = None,
        skip_ci: bool = False,
        as_draft: bool = False,
        **kwargs,
    ) -> dict[str, Any] | None:
        """
        Save the changes made by the agent to the repository.

        Args:
            branch_name: The branch name to commit and push the changes to. If None, the branch name will be
                generated based on the diff.
            merge_request_id: The merge request ID. If None, a new merge request will be created.
            skip_ci: Whether to skip the CI.
            as_draft: Whether to create the merge request as a draft.

        Returns:
            The branch name and merge request ID.
        """
        git_manager = GitManager(self.ctx.repo)

        if not git_manager.is_dirty():
            return None

        pr_metadata = await self._get_mr_metadata(git_manager.get_diff())
        branch_name = branch_name or pr_metadata.branch

        logger.info("Committing and pushing changes to branch '%s'", branch_name)

        unique_branch_name = git_manager.commit_and_push_changes(
            pr_metadata.commit_message, branch_name=branch_name, skip_ci=skip_ci, use_branch_if_exists=bool(branch_name)
        )

        if self.ctx.scope != Scope.MERGE_REQUEST and not merge_request_id:
            logger.info("Creating merge request: '%s' -> '%s'", unique_branch_name, self.ctx.config.default_branch)
            merge_request = self._update_or_create_merge_request(
                unique_branch_name, pr_metadata.title, pr_metadata.description
            )
            merge_request_id = merge_request.merge_request_id
            logger.info("Merge request created: %s", merge_request.web_url)
        return {"branch_name": unique_branch_name, "merge_request_id": merge_request_id}

    async def _get_mr_metadata(self, diff: str) -> PullRequestMetadata:
        """
        Get the PR metadata from the diff.

        Args:
            ctx: The runtime context.
            diff: The diff of the changes.

        Returns:
            The pull request metadata.
        """
        pr_describer = create_pr_describer_agent(model=self.ctx.config.models.pr_describer.model, ctx=self.ctx)

        extra_context = ""
        if self.ctx.scope == Scope.ISSUE:
            extra_context = dedent(
                """\
                This changes were made to address the following issue:

                Issue ID: {issue.iid}
                Issue title: {issue.title}
                Issue description: {issue.description}
                """
            ).format(issue=self.ctx.issue)

        result = await pr_describer.ainvoke(
            {"diff": redact_diff_content(diff, self.ctx.config.omit_content_patterns), "extra_context": extra_context},
            config={
                "tags": [pr_describer.get_name(), self.ctx.git_platform.value],
                "metadata": {"scope": self.ctx.scope, "repo_id": self.ctx.repo_id},
            },
        )
        if result and "structured_response" in result:
            return result["structured_response"]

        raise ValueError("Failed to get PR metadata from the diff.")

    def _update_or_create_merge_request(
        self, branch_name: str, title: str, description: str, as_draft: bool = False
    ) -> MergeRequest:
        """
        Update or create the merge request.

        Args:
            branch_name: The branch name.
            title: The title of the merge request.
            description: The description of the merge request.
            as_draft: Whether to create the merge request as a draft.

        Returns:
            The merge request.
        """
        assignee_id = None

        if self.ctx.issue and self.ctx.issue.assignee:
            assignee_id = (
                self.ctx.issue.assignee.id
                if self.ctx.git_platform == GitPlatform.GITLAB
                else self.ctx.issue.assignee.username
            )

        client = RepoClient.create_instance()
        return client.update_or_create_merge_request(
            repo_id=self.ctx.repo_id,
            source_branch=branch_name,
            target_branch=cast("str", self.ctx.config.default_branch),
            labels=[BOT_LABEL],
            title=title,
            assignee_id=assignee_id,
            as_draft=as_draft,
            description=render_to_string(
                "codebase/issue_merge_request.txt",
                {
                    "description": description,
                    "source_repo_id": self.ctx.repo_id,
                    "issue_id": self.ctx.issue.iid if self.ctx.issue else None,
                    "bot_name": BOT_NAME,
                    "bot_username": self.ctx.bot_username,
                    "is_gitlab": self.ctx.git_platform == GitPlatform.GITLAB,
                },
            ),
        )
