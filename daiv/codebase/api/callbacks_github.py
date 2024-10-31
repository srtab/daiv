import logging
from functools import cached_property
from typing import Any, Literal

from django.core.cache import cache

from asgiref.sync import sync_to_async

from codebase.api.callbacks import BaseCallback
from codebase.api.models import Issue, IssueAction, PullRequest, Note, NoteableType, NoteAction, Repository, User
from codebase.clients import RepoClient
from codebase.tasks import address_issue_task, address_review_task, update_index_repository
from core.config import RepositoryConfig

logger = logging.getLogger(__name__)


class IssueCallback(BaseCallback):
    """
    GitHub Issue Webhook
    """

    action: Literal["opened", "edited", "deleted"]
    repository: Repository
    sender: User
    issue: Issue
    changes: dict

    def accept_callback(self) -> bool:
        return (
            RepositoryConfig.get_config(self.repository.full_name).features.auto_address_issues_enabled
            and self.action in ["opened", "edited"]
            # Only accept if there are changes in the title or description of the issue.
            and (self.changes and "title" in self.changes or "body" in self.changes or "labels" in self.changes)
            # Only accept if the issue is a DAIV issue.
            and self.issue.is_daiv()
            and self.issue.state == "open"
        )

    async def process_callback(self):
        cache_key = f"{self.repository.full_name}:{self.issue.number}"
        with await cache.alock(f"{cache_key}::lock", timeout=300, blocking_timeout=30):
            if await cache.aget(cache_key) is None:
                await cache.aset(cache_key, "launched", timeout=60 * 10)
                address_issue_task.si(
                    repo_id=self.repository.full_name,
                    issue_iid=self.issue.number,
                    should_reset_plan=self.should_reset_plan(),
                    lock_cache_key=cache_key,
                ).apply_async()
            else:
                logger.warning(
                    "Issue %s is already being processed. Skipping the webhook processing.", self.issue.number
                )

    def should_reset_plan(self) -> bool:
        """
        Check if the plan should be reset.
        """
        return bool(
            self.action == "edited"
            and self.changes
            and any(field_name in self.changes for field_name in ["title", "body"])
        )


class NoteCallback(BaseCallback):
    """
    GitHub Note Webhook
    """

    action: Literal["created", "edited", "deleted"]
    repository: Repository
    sender: User
    pull_request: PullRequest | None = None
    issue: Issue | None = None
    comment: Note

    def model_post_init(self, __context: Any):
        self._repo_config = RepositoryConfig.get_config(self.repository.full_name)

    def accept_callback(self) -> bool:
        """
        Accept the webhook if the note is a review feedback for a pull request.
        """
        client = RepoClient.create_instance()
        return bool(
            (
                self._repo_config.features.auto_address_issues_enabled
                or self._repo_config.features.auto_address_review_enabled
            )
            and self.sender.id != client.current_user.id
            and not self.comment.system
            and self.action == "created"
            and (
                (
                    self.comment.noteable_type == NoteableType.PULL_REQUEST
                    and self.pull_request
                    and self.pull_request.state == "open"
                    and self.pull_request.is_daiv()
                )
                or (
                    self.comment.noteable_type == NoteableType.ISSUE
                    and self.issue
                    and self.issue.is_daiv()
                    and self.issue.state == "open"
                )
            )
        )

    async def process_callback(self):
        """
        Trigger the task to address the review feedback or issue comment.

        GitHub Note Webhook is called multiple times, one per note/discussion.
        We need to prevent multiple webhook processing for the same pull request.
        """
        if self._repo_config.features.auto_address_issues_enabled and self.issue:
            cache_key = f"{self.repository.full_name}:{self.issue.number}"
            with await cache.alock(f"{cache_key}::lock", timeout=300, blocking_timeout=30):
                if await cache.aget(cache_key) is None:
                    await cache.aset(cache_key, "launched", timeout=60 * 10)
                    address_issue_task.si(
                        repo_id=self.repository.full_name, issue_iid=self.issue.number, lock_cache_key=cache_key
                    ).apply_async()
                else:
                    logger.warning(
                        "Issue %s is already being processed. Skipping the webhook processing.", self.issue.number
                    )

        if self._repo_config.features.auto_address_review_enabled and self.pull_request:
            cache_key = f"{self.repository.full_name}:{self.pull_request.number}"
            with await cache.alock(f"{cache_key}::lock", timeout=300, blocking_timeout=30):
                if await cache.aget(cache_key) is None:
                    await cache.aset(cache_key, "launched", timeout=60 * 10)
                    address_review_task.si(
                        repo_id=self.repository.full_name,
                        pull_request_id=self.pull_request.number,
                        pull_request_source_branch=self.pull_request.head.ref,
                        lock_cache_key=cache_key,
                    ).apply_async()
                else:
                    logger.warning(
                        "Pull request %s is already being processed. Skipping the webhook processing.",
                        self.pull_request.number,
                    )


class PushCallback(BaseCallback):
    """
    GitHub Push Webhook for automatically update the codebase index.
    """

    ref: str
    repository: Repository
    after: str

    def accept_callback(self) -> bool:
        """
        Accept the webhook if the push is to the default branch or to any branch with PR created.
        """
        return self.ref.endswith(self.repository.default_branch) or bool(self.related_pull_requests)

    async def process_callback(self):
        """
        Process the push webhook to update the codebase index and invalidate the cache for the
        repository configurations.
        """
        if self.ref.endswith(self.repository.default_branch):
            # Invalidate the cache for the repository configurations, they could have changed.
            RepositoryConfig.invalidate_cache(self.repository.full_name)
            await sync_to_async(
                update_index_repository.si(self.repository.full_name, self.repository.default_branch).delay
            )()

        for pull_request in self.related_pull_requests:
            await sync_to_async(
                update_index_repository.si(self.repository.full_name, pull_request.head.ref).delay
            )()

    @cached_property
    def related_pull_requests(self) -> list[PullRequest]:
        """
        Get the pull requests related to the push.
        """
        client = RepoClient.create_instance()
        return client.get_commit_related_pull_requests(self.repository.full_name, commit_sha=self.after)
