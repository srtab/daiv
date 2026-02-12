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

from .diff_to_metadata.graph import create_diff_to_metadata_graph

if TYPE_CHECKING:
    from codebase.context import RuntimeCtx


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
        self.client = RepoClient.create_instance()

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
        self, *, merge_request: MergeRequest | None = None, skip_ci: bool = False, as_draft: bool = False, **kwargs
    ) -> MergeRequest | None:
        """
        Save the changes made by the agent to the repository.

        Args:
            merge_request: The merge request to commit and push the changes to. If None, a new merge request will be
                generated based on the diff.
            skip_ci: Whether to skip the CI.
            as_draft: Whether to create the merge request as a draft if merge request doesn't exist.

        Returns:
            The merge request if it was created or updated, otherwise None.
        """
        git_manager = GitManager(self.ctx.repo)

        if not git_manager.is_dirty():
            logger.info("No changes to publish.")
            return None

        # Compute full diff metadata when creating a new merge request or updating a draft merge request
        # to ensure we have the most up-to-date information.
        pr_metadata_diff = (
            git_manager.get_diff(f"origin/{self.ctx.config.default_branch}")
            if merge_request is None or (merge_request.draft and as_draft is False)
            else None
        )

        changes_metadata = await self._diff_to_metadata(
            pr_metadata_diff=pr_metadata_diff, commit_message_diff=git_manager.get_diff()
        )

        unique_branch_name = git_manager.commit_and_push_changes(
            changes_metadata["commit_message"].commit_message,
            branch_name=(
                changes_metadata["pr_metadata"].branch if merge_request is None else merge_request.source_branch
            ),
            use_branch_if_exists=merge_request is not None,
            skip_ci=skip_ci,
        )

        logger.info("Published changes to branch: '%s' [skip_ci: %s]", unique_branch_name, skip_ci)

        if merge_request is None:
            merge_request = self._create_merge_request(
                unique_branch_name,
                changes_metadata["pr_metadata"].title,
                changes_metadata["pr_metadata"].description,
                as_draft=as_draft,
            )
            logger.info(
                "Created merge request: %s [merge_request_id: %s, draft: %r]",
                merge_request.web_url,
                merge_request.merge_request_id,
                merge_request.draft,
            )
        elif merge_request.draft and as_draft is False:
            merge_request = self.client.update_merge_request(
                merge_request.repo_id, merge_request.merge_request_id, as_draft=as_draft
            )
            logger.info(
                "Updated merge request: %s [merge_request_id: %s, draft: %r]",
                merge_request.web_url,
                merge_request.merge_request_id,
                merge_request.draft,
            )

        return merge_request

    async def _diff_to_metadata(self, commit_message_diff: str, pr_metadata_diff: str | None = None) -> dict[str, Any]:
        """
        Get the PR metadata from the diff.

        Args:
            ctx: The runtime context.
            commit_message_diff: The diff of the commit message.
            pr_metadata_diff: The diff of the PR metadata. If None, the PR metadata will not be computed.

        Returns:
            The pull request metadata and commit message.
        """

        input_data = {
            "commit_message_diff": redact_diff_content(commit_message_diff, self.ctx.config.omit_content_patterns)
        }
        if self.ctx.scope == Scope.ISSUE:
            input_data["extra_context"] = dedent(
                """\
                This changes were made to address the following issue:

                Issue ID: {issue.iid}
                Issue title: {issue.title}
                Issue description: {issue.description}
                """
            ).format(issue=self.ctx.issue)

        if pr_metadata_diff:
            input_data["pr_metadata_diff"] = redact_diff_content(
                pr_metadata_diff, self.ctx.config.omit_content_patterns
            )

        changes_metadata_graph = create_diff_to_metadata_graph(ctx=self.ctx, include_pr_metadata=bool(pr_metadata_diff))
        result = await changes_metadata_graph.ainvoke(
            input_data,
            config={
                "tags": [self.ctx.git_platform.value],
                "metadata": {"scope": self.ctx.scope, "repo_id": self.ctx.repo_id},
            },
        )
        if result and ("pr_metadata" in result or "commit_message" in result):
            return result

        raise ValueError("Failed to get PR metadata from the diff.")

    def _create_merge_request(
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

        return self.client.update_or_create_merge_request(
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
