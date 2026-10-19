import logging
from functools import cached_property
from typing import Any, Literal

from sessions.pipeline_watch.judgment import JUDGEABLE_PIPELINE_STATUSES
from sessions.pipeline_watch.policy import WatchPolicy
from sessions.pipeline_watch.service import PipelineWatch

from codebase.clients import RepoClient
from codebase.clients.base import Emoji
from codebase.repo_config import RepositoryConfig
from codebase.utils import note_mentions_daiv
from core.constants import BOT_MAX_LABEL, has_bot_label
from webhooks.callbacks import BaseCallback
from webhooks.dispatch import Author, Mention, dispatch_issue_run, dispatch_mr_comment_run

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
    PipelineEvent,
    Project,
    User,
)

logger = logging.getLogger("daiv.webhooks")


def _author(user: User) -> Author:
    return Author(provider="gitlab", uid=user.id, username=user.username, email=user.email)


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

            previous_labels = {label.title.lower() for label in self.changes.labels.previous}
            added_labels = [
                label.title for label in self.changes.labels.current if label.title.lower() not in previous_labels
            ]
            if not has_bot_label(added_labels):
                logger.debug("No DAIV label was added in this update, ignoring")
                return False

        # For OPEN action, check if issue has DAIV label
        elif not has_bot_label(self.object_attributes.label_names):
            return False

        return True

    async def process_callback(self):
        """
        Trigger the task to address the issue.
        """
        await dispatch_issue_run(
            client=self._client,
            repo_id=self.project.path_with_namespace,
            issue_iid=self.object_attributes.iid,
            title=self.object_attributes.title,
            use_max=has_bot_label(self.object_attributes.label_names, among=[BOT_MAX_LABEL]),
            author=_author(self.user),
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
        mention = Mention(note_id=self.object_attributes.id, comment_id=self.object_attributes.discussion_id)
        if self.issue and self._is_issue_comment:
            await dispatch_issue_run(
                client=self._client,
                repo_id=self.project.path_with_namespace,
                issue_iid=self.issue.iid,
                title=self.issue.title,
                use_max=has_bot_label(self.issue.label_names, among=[BOT_MAX_LABEL]),
                author=_author(self.user),
                mention=mention,
            )
        elif self.merge_request and self._is_merge_request_comment:
            await dispatch_mr_comment_run(
                client=self._client,
                repo_id=self.project.path_with_namespace,
                merge_request_iid=self.merge_request.iid,
                ref=self.merge_request.source_branch,
                title=self.merge_request.title,
                use_max=has_bot_label(self.merge_request.label_names, among=[BOT_MAX_LABEL]),
                author=_author(self.user),
                mention=mention,
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


class PipelineCallback(BaseCallback):
    """
    GitLab Pipeline Webhook for babysitting CI on merge requests DAIV published.
    """

    object_kind: Literal["pipeline"]
    project: Project
    user: User
    object_attributes: PipelineEvent

    @cached_property
    def _repo_config(self) -> RepositoryConfig:
        return RepositoryConfig.get_config(self.project.path_with_namespace)

    def accept_callback(self) -> bool:
        """Accept pipelines the judge has an outcome for, on repos with the watch enabled.

        Deliberately does *not* reject pipelines attributed to DAIV: the service-account
        heal on ephemeral-token repos makes those the ones worth watching. Status is tested
        first because ``_repo_config`` blocks the event loop on a cache round-trip.
        """
        return self.object_attributes.status in JUDGEABLE_PIPELINE_STATUSES and WatchPolicy.enabled_for(
            self._repo_config
        )

    async def process_callback(self):
        await PipelineWatch(self.project.path_with_namespace).arequest_evaluation(
            ref=self.object_attributes.ref, pipeline_id=self.object_attributes.id
        )
