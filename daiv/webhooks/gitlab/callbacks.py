import logging
from functools import cached_property
from typing import Literal

from sessions.inbox.ownership import aclose_ownership, areopen_ownership, aschedule_push_classification
from sessions.pipeline_watch.judgment import JUDGEABLE_PIPELINE_STATUSES
from sessions.pipeline_watch.policy import WatchPolicy
from sessions.pipeline_watch.service import PipelineWatch

from codebase.base import GitPlatform, NoteType
from codebase.clients import RepoClient
from codebase.clients.base import Emoji
from codebase.repo_config import RepositoryConfig
from codebase.utils import note_mentions_daiv
from core.constants import has_bot_label, has_max_label
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
    return Author(provider=GitPlatform.GITLAB, uid=user.id, username=user.username, email=user.email)


class GitLabCallback(BaseCallback):
    """
    Base class for GitLab callbacks.
    """

    project: Project

    @cached_property
    def _repo_config(self) -> RepositoryConfig:
        return RepositoryConfig.get_config(self.project.path_with_namespace)

    @property
    def _client(self) -> RepoClient:
        return RepoClient.create_instance()


class IssueCallback(GitLabCallback):
    """
    Gitlab Issue Webhook
    """

    object_kind: Literal["issue", "work_item"]
    user: User
    object_attributes: Issue
    changes: IssueChanges | None = None

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
            use_max=has_max_label(self.object_attributes.label_names),
            author=_author(self.user),
        )


class NoteCallback(GitLabCallback):
    """
    Gitlab Note Webhook
    """

    object_kind: Literal["note"]
    user: User
    merge_request: MergeRequest | None = None
    issue: Issue | None = None
    object_attributes: Note

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
                use_max=has_max_label(self.issue.label_names),
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
                use_max=has_max_label(self.merge_request.label_names),
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
            and self.object_attributes.type in (None, NoteType.DISCUSSION_NOTE)
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


class MergeRequestCallback(GitLabCallback):
    """
    Gitlab Merge Request Webhook: merge metrics for merges into the default branch, and the merge, close and reopen of
    a merge request a session owns.
    """

    object_kind: Literal["merge_request"]
    user: User
    object_attributes: MergeRequestEvent

    def accept_callback(self) -> bool:
        """
        Accept a merge, a close or a reopen; ``process_callback`` decides what each one concerns.
        """
        return self.object_attributes.action in (
            MergeRequestAction.MERGE,
            MergeRequestAction.CLOSE,
            MergeRequestAction.REOPEN,
        )

    @property
    def _merged_into_default_branch(self) -> bool:
        return (
            self.object_attributes.action == MergeRequestAction.MERGE
            and self.object_attributes.state == "merged"
            and bool(self.project.default_branch)
            and self.object_attributes.target_branch == self.project.default_branch
        )

    async def process_callback(self):
        """
        Record merge metrics for a merge into the default branch, then tell the session owning the merge request.
        """
        if self._merged_into_default_branch:
            await self._record_merge_metrics()
        repo_id, iid = self.project.path_with_namespace, self.object_attributes.iid
        if self.object_attributes.action == MergeRequestAction.REOPEN:
            await areopen_ownership(repo_id, iid, by_username=self.user.username)
        else:
            await aclose_ownership(
                repo_id,
                iid,
                merged=self.object_attributes.action == MergeRequestAction.MERGE,
                by_username=self.user.username,
                target_branch=self.object_attributes.target_branch,
            )

    async def _record_merge_metrics(self):
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


class PushCallback(GitLabCallback):
    """
    Gitlab Push Webhook: a push to the default branch invalidates the repository configuration cache, and a push to
    the branch of a merge request a session owns is weighed for that session.
    """

    object_kind: Literal["push"]
    checkout_sha: str | None = None
    ref: str
    before: str = ""
    after: str = ""
    user_username: str = ""
    total_commits_count: int = 0

    def accept_callback(self) -> bool:
        """
        Accept a push to a branch; a tag push concerns neither the configuration nor a merge request.
        """
        return self.ref.startswith("refs/heads/")

    async def process_callback(self):
        branch = self.ref.removeprefix("refs/heads/")
        if branch == self.project.default_branch:
            RepositoryConfig.invalidate_cache(self.project.path_with_namespace)
        elif self.after and set(self.after) != {"0"}:
            await aschedule_push_classification(
                self.project.path_with_namespace,
                branch=branch,
                before=self.before,
                after=self.after,
                pusher_username=self.user_username,
                commit_count=self.total_commits_count,
            )


class PipelineCallback(GitLabCallback):
    """
    GitLab Pipeline Webhook for babysitting CI on merge requests DAIV published.
    """

    object_kind: Literal["pipeline"]
    user: User
    object_attributes: PipelineEvent

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
