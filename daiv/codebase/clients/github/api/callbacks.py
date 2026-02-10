import logging
from functools import cached_property
from typing import Any, Literal

from codebase.api.callbacks import BaseCallback
from codebase.clients import RepoClient
from codebase.clients.base import Emoji
from codebase.repo_config import RepositoryConfig
from codebase.tasks import address_issue_task, address_mr_comments_task, address_mr_review_task
from codebase.utils import note_mentions_daiv
from core.constants import BOT_AUTO_LABEL, BOT_LABEL, BOT_MAX_LABEL

from .models import Comment, Issue, Label, PullRequest, Repository, Review  # noqa: TC001

logger = logging.getLogger("daiv.webhooks")


class GitHubCallback(BaseCallback):
    """
    Base class for GitHub callbacks.
    """

    repository: Repository


class IssueCallback(GitHubCallback):
    """
    GitHub Issue Webhook for automatically address the issue.
    """

    action: Literal["opened", "edited", "reopened", "labeled", "closed"]
    issue: Issue
    label: Label | None = None

    def model_post_init(self, __context: Any):
        self._repo_config = RepositoryConfig.get_config(self.repository.full_name)
        self._client = RepoClient.create_instance()

    def accept_callback(self) -> bool:
        # Check basic conditions
        if not (
            self._repo_config.issue_addressing.enabled
            and self.issue.state == "open"
            and self.action in ["opened", "reopened", "labeled"]
        ):
            return False

        # Check if DAIV has already reacted to the issue (prevents re-launching when label is removed and re-added)
        if self._client.has_issue_reaction(self.repository.full_name, self.issue.number, Emoji.EYES):
            logger.info(
                "Skipping issue %s#%s: DAIV has already reacted to this issue",
                self.repository.full_name,
                self.issue.number,
            )
            return False

        # For labeled action, verify that a DAIV label was added
        if self.action == "labeled":
            if self.label is None:
                logger.warning("Labeled action received but no label field in payload")
                return False

            daiv_labels = {BOT_LABEL.lower(), BOT_AUTO_LABEL.lower(), BOT_MAX_LABEL.lower()}
            if self.label.name.lower() not in daiv_labels:
                logger.debug("Label %s is not a DAIV label, ignoring", self.label.name)
                return False

        # For opened/reopened, check if issue has DAIV label
        elif not self.issue.is_daiv():
            return False

        return True

    async def process_callback(self):
        self._client.create_issue_emoji(self.repository.full_name, self.issue.number, Emoji.EYES)
        await address_issue_task.aenqueue(repo_id=self.repository.full_name, issue_iid=self.issue.number)


class IssueCommentCallback(GitHubCallback):
    """
    GitHub Note Webhook for automatically address the review feedback on an pull request or process slash commands.
    """

    action: Literal["created", "edited", "deleted"]
    issue: Issue
    comment: Comment

    def model_post_init(self, __context: Any):
        self._repo_config = RepositoryConfig.get_config(self.repository.full_name)
        self._client = RepoClient.create_instance()

    def accept_callback(self) -> bool:
        """
        Check if the webhook is accepted.
        """
        if (
            self.action not in ["created", "edited"]
            or self.issue.state != "open"
            or self.comment.user.id == self._client.current_user.id
        ):
            return False

        return bool(self._is_issue_comment or self._is_merge_request_review)

    async def process_callback(self):
        """
        Trigger the task to address the review feedback or issue comment like the plan approval use case.
        """
        if self._is_issue_comment:
            self._client.create_issue_emoji(self.repository.full_name, self.issue.number, Emoji.EYES, self.comment.id)
            await address_issue_task.aenqueue(
                repo_id=self.repository.full_name, issue_iid=self.issue.number, mention_comment_id=str(self.comment.id)
            )

        elif self._is_merge_request_review:
            self._client.create_merge_request_note_emoji(
                self.repository.full_name, self.issue.number, Emoji.EYES, self.comment.id
            )
            await address_mr_comments_task.aenqueue(
                repo_id=self.repository.full_name,
                merge_request_id=self.issue.number,
                mention_comment_id=str(self.comment.id),
            )

    @property
    def _is_merge_request_review(self) -> bool:
        """
        Accept the webhook if the note is a merge request comment that mentions DAIV.
        """
        return bool(
            self._repo_config.code_review.enabled
            and self.issue.is_pull_request()
            and self.issue.state == "open"
            # and not self.issue.draft
            and self.action in ["created", "edited"]
            and note_mentions_daiv(self.comment.body, self._client.current_user)
        )

    @cached_property
    def _is_issue_comment(self) -> bool:
        """
        Accept the webhook if the note is an issue comment that mentions DAIV.
        """
        return bool(
            self._repo_config.issue_addressing.enabled
            and self.issue.is_issue()
            and self.issue.state == "open"
            and self.action in ["created", "edited"]
            and note_mentions_daiv(self.comment.body, self._client.current_user)
        )


class PullRequestReviewCallback(GitHubCallback):
    """
    GitHub Pull Request Review Webhook for automatically address the review feedback.
    """

    action: Literal["submitted", "edited", "dismissed"]
    pull_request: PullRequest
    review: Review

    def model_post_init(self, __context: Any):
        self._client = RepoClient.create_instance()

    def accept_callback(self) -> bool:
        """
        Check if the webhook is accepted.
        """
        return (
            self.action in ["submitted", "edited"]
            and self.pull_request.state == "open"
            # Ignore the DAIV review itself
            and self.review.user.id != self._client.current_user.id
        )

    async def process_callback(self):
        """
        Trigger the task to address the review feedback or issue comment like the plan approval use case.

        GitLab Note Webhook is called multiple times, one per note/discussion.
        """
        await address_mr_review_task.aenqueue(
            repo_id=self.repository.full_name,
            merge_request_id=self.pull_request.number,
            merge_request_source_branch=self.pull_request.head.ref,
        )


class PushCallback(GitHubCallback):
    """
    GitHub Push Webhook for automatically invalidate the cache for the repository configurations.
    """

    ref: str

    def accept_callback(self) -> bool:
        """
        Accept the webhook if the push is to the default branch.
        """
        return self.ref.endswith(self.repository.default_branch)

    async def process_callback(self):
        """
        Process the push webhook to invalidate the cache for the repository configurations.
        """
        if self.repository.default_branch and self.ref.endswith(self.repository.default_branch):
            # Invalidate the cache for the repository configurations, they could have changed.
            RepositoryConfig.invalidate_cache(self.repository.full_name)
