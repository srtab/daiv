import logging
from functools import cached_property
from typing import Any, Literal

from codebase.api.callbacks import BaseCallback
from codebase.base import NoteType
from codebase.clients import RepoClient
from codebase.clients.base import Emoji
from codebase.repo_config import RepositoryConfig
from codebase.tasks import address_issue_task, address_mr_comments_task, address_mr_review_task
from codebase.utils import note_mentions_daiv

from .models import Issue, IssueAction, MergeRequest, Note, NoteableType, NoteAction, Project, User

logger = logging.getLogger("daiv.webhooks")


class IssueCallback(BaseCallback):
    """
    Gitlab Issue Webhook
    """

    object_kind: Literal["issue", "work_item"]
    project: Project
    object_attributes: Issue

    def accept_callback(self) -> bool:
        return (
            RepositoryConfig.get_config(self.project.path_with_namespace).issue_addressing.enabled
            and self.object_attributes.action in [IssueAction.OPEN, IssueAction.UPDATE]
            # Only accept if the issue is a DAIV issue.
            and self.object_attributes.is_daiv()
            # When work_item is created without the parent issue, the object_kind=issue.
            # We need to check the type too to avoid processing work_items as issues.
            and self.object_kind == "issue"
            and self.object_attributes.type == "Issue"
            and self.object_attributes.state == "opened"
        )

    async def process_callback(self):
        """
        Trigger the task to address the issue.
        """
        await address_issue_task.aenqueue(
            repo_id=self.project.path_with_namespace, issue_iid=self.object_attributes.iid
        )


class NoteCallback(BaseCallback):
    """
    Gitlab Note Webhook
    """

    object_kind: Literal["note"]
    project: Project
    user: User
    merge_request: MergeRequest | None = None
    issue: Issue | None = None
    object_attributes: Note

    def model_post_init(self, __context: Any):
        self._repo_config = RepositoryConfig.get_config(self.project.path_with_namespace)
        self._client = RepoClient.create_instance()

    def accept_callback(self) -> bool:
        """
        Check if the webhook is accepted.
        """
        if (
            self.object_attributes.noteable_type not in [NoteableType.ISSUE, NoteableType.MERGE_REQUEST]
            or self.object_attributes.system
            or self.user.id == self._client.current_user.id
        ):
            return False

        return bool(self._is_issue_comment or self._is_merge_request_review)

    async def process_callback(self):
        """
        Trigger the task to address the review feedback, issue comment or slash command.

        GitLab Note Webhook is called multiple times, one per note/discussion.
        """
        if self._is_issue_comment:
            self._client.create_issue_note_emoji(
                self.project.path_with_namespace, self.issue.iid, Emoji.EYES, self.object_attributes.id
            )
            await address_issue_task.aenqueue(
                repo_id=self.project.path_with_namespace,
                issue_iid=self.issue.iid,
                mention_comment_id=self.object_attributes.discussion_id,
            )

        elif self._is_merge_request_review:
            if self.object_attributes.type in [NoteType.DIFF_NOTE, NoteType.DISCUSSION_NOTE]:
                await address_mr_review_task.aenqueue(
                    repo_id=self.project.path_with_namespace,
                    merge_request_id=self.merge_request.iid,
                    merge_request_source_branch=self.merge_request.source_branch,
                )
            elif self.object_attributes.type is None:  # This is a comment note.
                await address_mr_comments_task.aenqueue(
                    repo_id=self.project.path_with_namespace,
                    merge_request_id=self.merge_request.iid,
                    merge_request_source_branch=self.merge_request.source_branch,
                    mention_comment_id=self.object_attributes.discussion_id,
                )
            else:
                logger.warning("Unsupported note type: %s", self.object_attributes.type)

    @cached_property
    def _is_merge_request_review(self) -> bool:
        """
        Accept the webhook if the note is a merge request comment that mentions DAIV.
        """
        return bool(
            self._repo_config.code_review.enabled
            and self.object_attributes.noteable_type == NoteableType.MERGE_REQUEST
            and self.object_attributes.action in [NoteAction.CREATE, NoteAction.UPDATE]
            and self.merge_request
            and self.merge_request.state == "opened"
            and note_mentions_daiv(self.object_attributes.note, self._client.current_user)
        )

    @cached_property
    def _is_issue_comment(self) -> bool:
        """
        Accept the webhook if the note is an issue comment that mentions DAIV.
        """
        return bool(
            self._repo_config.issue_addressing.enabled
            and self.object_attributes.noteable_type == NoteableType.ISSUE
            and self.object_attributes.action == NoteAction.CREATE
            and self.issue
            and self.issue.state == "opened"
            and note_mentions_daiv(self.object_attributes.note, self._client.current_user)
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
        return self.ref.endswith(self.project.default_branch)

    async def process_callback(self):
        """
        Process the push webhook to update the codebase index and invalidate the cache for the
        repository configurations.
        """
        if self.project.default_branch and self.ref.endswith(self.project.default_branch):
            # Invalidate the cache for the repository configurations, they could have changed.
            RepositoryConfig.invalidate_cache(self.project.path_with_namespace)
