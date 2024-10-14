import logging
from functools import cached_property
from typing import Literal

from django.core.cache import cache

from codebase.api.callbacks import BaseCallback
from codebase.api.models import Issue, IssueAction, MergeRequest, Note, NoteableType, NoteAction, Project, User
from codebase.clients import RepoClient
from codebase.tasks import address_issue_task, handle_mr_feedback, update_index_repository

logger = logging.getLogger(__name__)


class IssueCallback(BaseCallback):
    """
    Gitlab Issue Webhook
    """

    object_kind: Literal["issue", "work_item"]
    project: Project
    user: User
    object_attributes: Issue
    changes: dict

    def accept_callback(self) -> bool:
        return (
            self.object_attributes.action in [IssueAction.OPEN, IssueAction.UPDATE]
            # Only accept if there are changes in the title or description of the issue.
            and (self.changes and "title" in self.changes or "description" in self.changes or "labels" in self.changes)
            # Only accept if the issue is a DAIV issue.
            and self.object_attributes.is_daiv()
            # When work_item is created without the parent issue, the object_kind=issue.
            # We need to check the type too to avoid processing work_items as issues.
            and self.object_kind == "issue"
            and self.object_attributes.type == "Issue"
        )

    async def process_callback(self):
        cache_key = f"{self.project.path_with_namespace}:{self.object_attributes.iid}"
        with await cache.alock(f"{cache_key}::lock", timeout=300, blocking_timeout=30):
            if await cache.aget(cache_key) is None:
                await cache.aset(cache_key, "launched", timeout=60 * 10)
                # handle_mr_feedback.si(
                address_issue_task(
                    repo_id=self.project.path_with_namespace,
                    ref=self.project.default_branch,
                    issue_id=self.object_attributes.iid,
                    should_reset_plan=self.should_reset_plan(),
                    cache_key=cache_key,
                )
                # ).apply_async()
            else:
                logger.info(
                    "Issue %s is already being processed. Skipping the webhook processing.", self.object_attributes.iid
                )

    def should_reset_plan(self) -> bool:
        """
        Check if the plan should be reset.
        """
        return bool(
            self.object_attributes.action == IssueAction.UPDATE
            and self.changes
            and any(field_name in self.changes for field_name in ["title", "description"])
        )


class NoteCallback(BaseCallback):
    """
    Gitlab Note Webhook
    """

    object_kind: Literal["note"]
    project: Project
    user: User
    merge_request: MergeRequest | None = None
    object_attributes: Note

    def accept_callback(self) -> bool:
        """
        Accept the webhook if the note is a review feedback for a merge request.
        """
        client = RepoClient.create_instance()
        return bool(
            self.object_attributes.noteable_type == NoteableType.MERGE_REQUEST
            and self.user.id != client.current_user.id
            and not self.object_attributes.system
            and self.object_attributes.action == NoteAction.CREATE
            and self.merge_request
            and not self.merge_request.work_in_progress
            and self.merge_request.state == "opened"
            and self.merge_request.is_daiv()
        )

    async def process_callback(self):
        """
        Process the webhook by generating the changes and committing them to the source branch.

        GitLab Note Webhook is called multiple times, one per note/discussion.
        We need to prevent multiple webhook processing for the same merge request.
        """
        cache_key = f"{self.project.path_with_namespace}:{self.merge_request.iid}"
        with await cache.alock(f"{cache_key}::lock", timeout=300, blocking_timeout=30):
            if await cache.aget(cache_key) is None:
                await cache.aset(cache_key, "launched", timeout=60 * 10)
                # handle_mr_feedback.si(
                handle_mr_feedback(
                    repo_id=self.project.path_with_namespace,
                    merge_request_id=self.merge_request.iid,
                    merge_request_source_branch=self.merge_request.source_branch,
                    cache_key=cache_key,
                )
                # ).apply_async()
            else:
                logger.info(
                    "Merge request %s is already being processed. Skipping the webhook processing.",
                    self.merge_request.iid,
                )


class PushCallback(BaseCallback):
    """
    Gitlab Push Webhook for automatically update the codebase index.
    """

    object_kind: Literal["push"]
    project: Project
    checkout_sha: str
    ref: str

    def accept_callback(self) -> bool:
        """
        Accept the webhook if the push is to the default branch or to any branch with MR created.
        """
        return self.ref.endswith(self.project.default_branch) or bool(self.related_merge_requests)

    async def process_callback(self):
        """
        Trigger the update of the codebase index.
        """
        for merge_request in self.related_merge_requests:
            update_index_repository.si(self.project.path_with_namespace, merge_request.source_branch).apply_async()

    @cached_property
    def related_merge_requests(self) -> list[MergeRequest]:
        """
        Get the related merge requests for the push.
        """
        client = RepoClient.create_instance()
        return client.get_commit_related_merge_requests(self.project.path_with_namespace, commit_sha=self.checkout_sha)
