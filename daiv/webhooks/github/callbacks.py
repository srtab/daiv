import logging
from functools import cached_property
from typing import Any, Literal

from sessions.pipeline_watch.judgment import JUDGEABLE_PIPELINE_STATUSES
from sessions.pipeline_watch.policy import WatchPolicy
from sessions.pipeline_watch.service import PipelineWatch

from codebase.clients import RepoClient
from codebase.clients.base import Emoji
from codebase.clients.github.client import github_conclusion_to_status
from codebase.repo_config import RepositoryConfig
from codebase.utils import note_mentions_daiv
from core.constants import BOT_AUTO_LABEL, BOT_LABEL, BOT_MAX_LABEL
from webhooks.callbacks import BaseCallback
from webhooks.dispatch import Author, Mention, dispatch_issue_run, dispatch_mr_comment_run

from .models import Comment, Issue, Label, PullRequest, Repository, User, WorkflowRun  # noqa: TC001

logger = logging.getLogger("daiv.webhooks")


def _author(user: User) -> Author:
    return Author(provider="github", uid=user.id, username=user.username)


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
    sender: User

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

        # Check if user is allowed to interact with DAIV
        if not self._repo_config.is_user_allowed(self.sender.username):
            logger.info(
                "Rejecting issue %s#%s: user '%s' is not in the allowed usernames list",
                self.repository.full_name,
                self.issue.number,
                self.sender.username,
            )
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
        await dispatch_issue_run(
            client=self._client,
            repo_id=self.repository.full_name,
            issue_iid=self.issue.number,
            title=self.issue.title,
            use_max=self.issue.has_max_label(),
            author=_author(self.sender),
        )


class IssueCommentCallback(GitHubCallback):
    """
    GitHub Issue Comment Webhook for addressing issue follow-ups and merge request review feedback.
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

        # Check if user is allowed to interact with DAIV
        if not self._repo_config.is_user_allowed(self.comment.user.username):
            logger.info(
                "Rejecting comment on %s#%s: user '%s' is not in the allowed usernames list",
                self.repository.full_name,
                self.issue.number,
                self.comment.user.username,
            )
            return False

        return bool(self._is_issue_comment or self._is_merge_request_review)

    async def process_callback(self):
        """
        Trigger the task to address the review feedback or issue comment like the plan approval use case.
        """
        mention = Mention(note_id=self.comment.id, comment_id=str(self.comment.id))
        if self._is_issue_comment:
            await dispatch_issue_run(
                client=self._client,
                repo_id=self.repository.full_name,
                issue_iid=self.issue.number,
                title=self.issue.title,
                use_max=self.issue.has_max_label(),
                author=_author(self.comment.user),
                mention=mention,
            )
        elif self._is_merge_request_review:
            await dispatch_mr_comment_run(
                client=self._client,
                repo_id=self.repository.full_name,
                merge_request_iid=self.issue.number,
                ref=None,
                title=self.issue.title,
                use_max=self.issue.has_max_label(),
                author=_author(self.comment.user),
                mention=mention,
            )

    @property
    def _is_merge_request_review(self) -> bool:
        """
        Accept the webhook if the note is a merge request comment that mentions DAIV.
        """
        return bool(
            self._repo_config.pull_request_assistant.enabled
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


class PullRequestCallback(GitHubCallback):
    """
    GitHub Pull Request Webhook for tracking merge metrics.
    """

    action: str
    pull_request: PullRequest

    def accept_callback(self) -> bool:
        """
        Accept the webhook only when a pull request is merged into the default branch.
        """
        return (
            self.action == "closed"
            and self.pull_request.merged
            and self.pull_request.base.ref == self.repository.default_branch
        )

    async def process_callback(self):
        """
        Enqueue a task to record merge metrics.
        """
        from codebase.tasks import record_merge_metrics_task

        await record_merge_metrics_task.aenqueue(
            repo_id=self.repository.full_name,
            merge_request_iid=self.pull_request.number,
            title=self.pull_request.title,
            source_branch=self.pull_request.head.ref,
            target_branch=self.pull_request.base.ref,
            merged_at=self.pull_request.merged_at or "",
            platform="github",
        )


class PushCallback(GitHubCallback):
    """
    GitHub Push Webhook for invalidating repository configuration cache.
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


class WorkflowRunCallback(GitHubCallback):
    """
    GitHub Actions workflow_run webhook for babysitting CI on pull requests DAIV published.
    """

    action: str
    workflow_run: WorkflowRun

    @cached_property
    def _repo_config(self) -> RepositoryConfig:
        return RepositoryConfig.get_config(self.repository.full_name)

    def accept_callback(self) -> bool:
        """Accept finished workflow runs on repos with the watch enabled.

        Deliberately does not reject runs DAIV's own push triggered — that is the normal case.
        Translating the conclusion puts both platforms on the judge's one vocabulary; the
        config read comes last because it blocks the event loop on a cache round-trip.
        """
        return (
            self.action == "completed"
            and github_conclusion_to_status(self.workflow_run.conclusion) in JUDGEABLE_PIPELINE_STATUSES
            and WatchPolicy.enabled_for(self._repo_config)
        )

    async def process_callback(self):
        await PipelineWatch(self.repository.full_name).arequest_evaluation(
            ref=self.workflow_run.head_branch, pipeline_id=self.workflow_run.id
        )
