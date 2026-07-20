import logging
from functools import cached_property
from typing import Any, Literal

from django.core.cache import cache

from asgiref.sync import sync_to_async
from github.GithubException import GithubException
from sandbox_envs.services import resolve_env_for_run
from sessions.models import SessionOrigin
from sessions.services import acreate_run

from accounts.utils import resolve_user
from codebase.api.callbacks import BaseCallback
from codebase.base import Scope
from codebase.clients import RepoClient
from codebase.clients.base import Emoji
from codebase.mr_state import _mr_state_cache_key
from codebase.repo_config import RepositoryConfig
from codebase.tasks import address_issue_task, address_mr_comments_task
from codebase.utils import compute_thread_id, note_mentions_daiv
from core.constants import BOT_AUTO_LABEL, BOT_LABEL, BOT_MAX_LABEL

from .models import Comment, Issue, Label, PullRequest, Repository, User  # noqa: TC001

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
        try:
            self._client.create_issue_emoji(self.repository.full_name, self.issue.number, Emoji.EYES)
        except GithubException:
            logger.warning(
                "Failed to add reaction to issue %s#%s", self.repository.full_name, self.issue.number, exc_info=True
            )
        thread_id = compute_thread_id(
            repo_slug=self.repository.full_name, scope=Scope.ISSUE, entity_iid=self.issue.number
        )
        # user=None: webhook triggers are not scoped to the commenter's USER envs (the webhook
        # fires for whoever interacted with the issue/PR, not the configured agent owner). Only
        # GLOBAL repo envs and the GLOBAL default apply — mirroring set_runtime_ctx's contract.
        sandbox_env = await resolve_env_for_run(user=None, repo_id=self.repository.full_name)
        sandbox_environment_id = str(sandbox_env.id) if sandbox_env is not None else None
        result = await address_issue_task.aenqueue(
            repo_id=self.repository.full_name,
            issue_iid=self.issue.number,
            thread_id=thread_id,
            sandbox_environment_id=sandbox_environment_id,
        )
        daiv_user = await resolve_user("github", self.sender.id, username=self.sender.username)
        try:
            await acreate_run(
                trigger_type=SessionOrigin.ISSUE_WEBHOOK,
                task_result_id=result.id,
                repo_id=self.repository.full_name,
                issue_iid=self.issue.number,
                use_max=self.issue.has_max_label(),
                user=daiv_user,
                external_username=self.sender.username,
                title=self.issue.title,
                thread_id=thread_id,
                sandbox_environment_id=sandbox_environment_id,
            )
        except Exception:
            logger.exception("Failed to create run for issue %s#%s", self.repository.full_name, self.issue.number)


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
        daiv_user = await resolve_user("github", self.comment.user.id, username=self.comment.user.username)

        if self._is_issue_comment:
            try:
                self._client.create_issue_emoji(
                    self.repository.full_name, self.issue.number, Emoji.EYES, self.comment.id
                )
            except GithubException:
                logger.warning("Failed to add reaction to issue comment %s", self.comment.id, exc_info=True)
            thread_id = compute_thread_id(
                repo_slug=self.repository.full_name, scope=Scope.ISSUE, entity_iid=self.issue.number
            )
            sandbox_env = await resolve_env_for_run(user=None, repo_id=self.repository.full_name)
            sandbox_environment_id = str(sandbox_env.id) if sandbox_env is not None else None
            result = await address_issue_task.aenqueue(
                repo_id=self.repository.full_name,
                issue_iid=self.issue.number,
                mention_comment_id=str(self.comment.id),
                thread_id=thread_id,
                sandbox_environment_id=sandbox_environment_id,
            )
            try:
                await acreate_run(
                    trigger_type=SessionOrigin.ISSUE_WEBHOOK,
                    task_result_id=result.id,
                    repo_id=self.repository.full_name,
                    issue_iid=self.issue.number,
                    mention_comment_id=str(self.comment.id),
                    use_max=self.issue.has_max_label(),
                    user=daiv_user,
                    external_username=self.comment.user.username,
                    title=self.issue.title,
                    thread_id=thread_id,
                    sandbox_environment_id=sandbox_environment_id,
                )
            except Exception:
                logger.exception(
                    "Failed to create run for issue comment %s#%s", self.repository.full_name, self.issue.number
                )

        elif self._is_merge_request_review:
            try:
                self._client.create_merge_request_note_emoji(
                    self.repository.full_name, self.issue.number, Emoji.EYES, self.comment.id
                )
            except GithubException:
                logger.warning("Failed to add reaction to PR comment %s", self.comment.id, exc_info=True)
            thread_id = compute_thread_id(
                repo_slug=self.repository.full_name, scope=Scope.MERGE_REQUEST, entity_iid=self.issue.number
            )
            sandbox_env = await resolve_env_for_run(user=None, repo_id=self.repository.full_name)
            sandbox_environment_id = str(sandbox_env.id) if sandbox_env is not None else None
            result = await address_mr_comments_task.aenqueue(
                repo_id=self.repository.full_name,
                merge_request_id=self.issue.number,
                mention_comment_id=str(self.comment.id),
                thread_id=thread_id,
                sandbox_environment_id=sandbox_environment_id,
            )
            # GitHub's issue_comment payload omits head.ref, so fetch the PR. If that
            # fails the activity is still useful without a branch — don't drop it.
            source_branch = ""
            try:
                pr = await sync_to_async(self._client.get_merge_request)(self.repository.full_name, self.issue.number)
                source_branch = pr.source_branch
            except Exception:
                logger.exception(
                    "Failed to resolve source branch for PR comment %s#%s", self.repository.full_name, self.issue.number
                )
            try:
                await acreate_run(
                    trigger_type=SessionOrigin.MR_WEBHOOK,
                    task_result_id=result.id,
                    repo_id=self.repository.full_name,
                    ref=source_branch,
                    merge_request_iid=self.issue.number,
                    mention_comment_id=str(self.comment.id),
                    use_max=self.issue.has_max_label(),
                    user=daiv_user,
                    external_username=self.comment.user.username,
                    title=self.issue.title,
                    thread_id=thread_id,
                    sandbox_environment_id=sandbox_environment_id,
                )
            except Exception:
                logger.exception(
                    "Failed to create run for PR comment %s#%s", self.repository.full_name, self.issue.number
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
        # Eagerly drop the cached live MR state so the merged PR leaves the console near-instantly
        # rather than after the TTL (AC7). Pure cache side effect — no accept-gate change, no new
        # stored state; the same posture as ``PushCallback`` invalidating the repo-config cache.
        cache.delete(_mr_state_cache_key(self.repository.full_name, self.pull_request.number))


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
