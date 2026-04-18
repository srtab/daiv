import logging
from functools import cached_property
from typing import Any, Literal

from activity.models import TriggerType
from activity.services import acreate_activity
from gitlab.exceptions import GitlabError

from accounts.utils import resolve_user
from codebase.api.callbacks import BaseCallback
from codebase.clients import RepoClient
from codebase.clients.base import Emoji
from codebase.repo_config import RepositoryConfig
from codebase.tasks import address_issue_task, address_mr_comments_task
from codebase.utils import note_mentions_daiv
from core.constants import BOT_AUTO_LABEL, BOT_LABEL, BOT_MAX_LABEL

from .models import (  # noqa: TC001
    Issue,
    IssueAction,
    IssueChanges,
    MergeRequest,
    MergeRequestAction,
    MergeRequestEvent,
    Note,
    NoteableType,
    NoteAction,
    Project,
    User,
)

logger = logging.getLogger("daiv.webhooks")


class IssueCallback(BaseCallback):
    """
    Gitlab Issue Webhook
    """

    object_kind: Literal["issue", "work_item"]
    project: Project
    user: User
    object_attributes: Issue
    changes: IssueChanges | None = None

    def model_post_init(self, __context: Any):
        self._repo_config = RepositoryConfig.get_config(self.project.path_with_namespace)
        self._client = RepoClient.create_instance()

    def accept_callback(self) -> bool:
        # Check basic conditions
        if not (
            self._repo_config.issue_addressing.enabled
            and self.object_kind == "issue"
            and self.object_attributes.type == "Issue"
            and self.object_attributes.state == "opened"
            and self.object_attributes.action in [IssueAction.OPEN, IssueAction.UPDATE]
        ):
            return False

        # Check if user is allowed to interact with DAIV
        if not self._repo_config.is_user_allowed(self.user.username):
            logger.info(
                "Rejecting issue %s#%s: user '%s' is not in the allowed usernames list",
                self.project.path_with_namespace,
                self.object_attributes.iid,
                self.user.username,
            )
            return False

        # Check if DAIV has already reacted to the issue (prevents re-launching when label is removed and re-added)
        if self._client.has_issue_reaction(self.project.path_with_namespace, self.object_attributes.iid, Emoji.EYES):
            logger.info(
                "Skipping issue %s#%s: DAIV has already reacted to this issue",
                self.project.path_with_namespace,
                self.object_attributes.iid,
            )
            return False

        # For UPDATE action, only accept if labels were changed and a DAIV label was added
        if self.object_attributes.action == IssueAction.UPDATE:
            if self.changes is None or self.changes.labels is None:
                logger.debug("Issue update without label changes, ignoring")
                return False

            # Check if a DAIV label was added
            daiv_labels = {BOT_LABEL.lower(), BOT_AUTO_LABEL.lower(), BOT_MAX_LABEL.lower()}
            previous_labels = {label.title.lower() for label in self.changes.labels.previous}
            current_labels = {label.title.lower() for label in self.changes.labels.current}

            # Find labels that were added
            added_labels = current_labels - previous_labels
            daiv_label_added = bool(added_labels & daiv_labels)

            if not daiv_label_added:
                logger.debug("No DAIV label was added in this update, ignoring")
                return False

        # For OPEN action, check if issue has DAIV label
        elif not self.object_attributes.is_daiv():
            return False

        return True

    async def process_callback(self):
        """
        Trigger the task to address the issue.
        """
        try:
            self._client.create_issue_emoji(self.project.path_with_namespace, self.object_attributes.iid, Emoji.EYES)
        except GitlabError:
            logger.warning("Failed to add reaction to issue %s", self.object_attributes.iid, exc_info=True)
        result = await address_issue_task.aenqueue(
            repo_id=self.project.path_with_namespace, issue_iid=self.object_attributes.iid
        )
        daiv_user = await resolve_user("gitlab", self.user.id, username=self.user.username, email=self.user.email)
        try:
            await acreate_activity(
                trigger_type=TriggerType.ISSUE_WEBHOOK,
                task_result_id=result.id,
                repo_id=self.project.path_with_namespace,
                issue_iid=self.object_attributes.iid,
                use_max=self.object_attributes.has_max_label(),
                user=daiv_user,
                external_username=self.user.username,
            )
        except Exception:
            logger.exception(
                "Failed to create activity for issue %s#%s",
                self.project.path_with_namespace,
                self.object_attributes.iid,
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

        # Check if user is allowed to interact with DAIV
        if not self._repo_config.is_user_allowed(self.user.username):
            logger.info(
                "Rejecting note on project %s: user '%s' is not in the allowed usernames list",
                self.project.path_with_namespace,
                self.user.username,
            )
            return False

        return bool(self._is_issue_comment or self._is_merge_request_comment)

    async def process_callback(self):
        """
        Trigger the task to address the review feedback, issue comment or slash command.

        GitLab Note Webhook is called multiple times, one per note/discussion.
        """
        daiv_user = await resolve_user("gitlab", self.user.id, username=self.user.username, email=self.user.email)

        if self.issue and self._is_issue_comment:
            try:
                self._client.create_issue_emoji(
                    self.project.path_with_namespace, self.issue.iid, Emoji.EYES, self.object_attributes.id
                )
            except GitlabError:
                logger.warning("Failed to add reaction to issue comment %s", self.object_attributes.id, exc_info=True)
            result = await address_issue_task.aenqueue(
                repo_id=self.project.path_with_namespace,
                issue_iid=self.issue.iid,
                mention_comment_id=self.object_attributes.discussion_id,
            )
            try:
                await acreate_activity(
                    trigger_type=TriggerType.ISSUE_WEBHOOK,
                    task_result_id=result.id,
                    repo_id=self.project.path_with_namespace,
                    issue_iid=self.issue.iid,
                    mention_comment_id=self.object_attributes.discussion_id,
                    use_max=self.issue.has_max_label(),
                    user=daiv_user,
                    external_username=self.user.username,
                )
            except Exception:
                logger.exception(
                    "Failed to create activity for issue comment %s#%s",
                    self.project.path_with_namespace,
                    self.issue.iid,
                )

        elif self.merge_request and self._is_merge_request_comment:
            try:
                self._client.create_merge_request_note_emoji(
                    self.project.path_with_namespace, self.merge_request.iid, Emoji.EYES, self.object_attributes.id
                )
            except GitlabError:
                logger.warning("Failed to add reaction to MR comment %s", self.object_attributes.id, exc_info=True)
            result = await address_mr_comments_task.aenqueue(
                repo_id=self.project.path_with_namespace,
                merge_request_id=self.merge_request.iid,
                mention_comment_id=self.object_attributes.discussion_id,
            )
            try:
                await acreate_activity(
                    trigger_type=TriggerType.MR_WEBHOOK,
                    task_result_id=result.id,
                    repo_id=self.project.path_with_namespace,
                    merge_request_iid=self.merge_request.iid,
                    mention_comment_id=self.object_attributes.discussion_id,
                    use_max=self.merge_request.has_max_label(),
                    user=daiv_user,
                    external_username=self.user.username,
                )
            except Exception:
                logger.exception(
                    "Failed to create activity for MR comment %s#%s",
                    self.project.path_with_namespace,
                    self.merge_request.iid,
                )

    @cached_property
    def _is_merge_request_comment(self) -> bool:
        """
        Accept the webhook if the note is a merge request comment that mentions DAIV.
        """
        return bool(
            self._repo_config.pull_request_assistant.enabled
            and self.object_attributes.type is None  # This is a comment note.
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


class MergeRequestCallback(BaseCallback):
    """
    Gitlab Merge Request Webhook for tracking merge metrics.
    """

    object_kind: Literal["merge_request"]
    project: Project
    user: User
    object_attributes: MergeRequestEvent

    def accept_callback(self) -> bool:
        """
        Accept the webhook only when a merge request is merged into the default branch.
        """
        return (
            self.object_attributes.action == MergeRequestAction.MERGE
            and self.object_attributes.state == "merged"
            and bool(self.project.default_branch)
            and self.object_attributes.target_branch == self.project.default_branch
        )

    async def process_callback(self):
        """
        Enqueue a task to record merge metrics.
        """
        from codebase.tasks import record_merge_metrics_task

        await record_merge_metrics_task.aenqueue(
            repo_id=self.project.path_with_namespace,
            merge_request_iid=self.object_attributes.iid,
            title=self.object_attributes.title,
            source_branch=self.object_attributes.source_branch,
            target_branch=self.object_attributes.target_branch,
            merged_at=self.object_attributes.merged_at or "",
            platform="gitlab",
        )


class PushCallback(BaseCallback):
    """
    Gitlab Push Webhook for invalidating repository configuration cache.
    """

    object_kind: Literal["push"]
    project: Project
    checkout_sha: str
    ref: str

    def accept_callback(self) -> bool:
        """
        Accept the webhook if the push is to the default branch.
        """
        return bool(self.project.default_branch and self.ref.endswith(self.project.default_branch))

    async def process_callback(self):
        """
        Process the push webhook to invalidate the cache for the repository configurations.
        """
        RepositoryConfig.invalidate_cache(self.project.path_with_namespace)
