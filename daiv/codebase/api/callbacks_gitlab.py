import logging
from functools import cached_property
from typing import Any, Literal

from asgiref.sync import sync_to_async

from automation.quick_actions.base import Scope
from automation.quick_actions.parser import QuickActionCommand, parse_quick_action
from automation.quick_actions.registry import quick_action_registry
from automation.quick_actions.tasks import execute_quick_action_task
from codebase.api.callbacks import BaseCallback
from codebase.api.models import Issue, IssueAction, MergeRequest, Note, NoteableType, NoteAction, Project, User
from codebase.base import MergeRequest as BaseMergeRequest
from codebase.clients import RepoClient
from codebase.tasks import address_issue_task, address_review_task, update_index_repository
from codebase.utils import discussion_has_daiv_mentions, note_mentions_daiv
from core.config import RepositoryConfig

ISSUE_CHANGE_FIELDS = {"title", "description", "labels", "state_id"}

logger = logging.getLogger("daiv.webhooks")


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
            RepositoryConfig.get_config(self.project.path_with_namespace).features.auto_address_issues_enabled
            and self.object_attributes.action in [IssueAction.OPEN, IssueAction.UPDATE]
            # Only accept if there are changes in the title, description, labels or state of the issue.
            and bool(self.changes.keys() & ISSUE_CHANGE_FIELDS)
            # Only accept if the issue is a DAIV issue.
            and self.object_attributes.is_daiv()
            # When work_item is created without the parent issue, the object_kind=issue.
            # We need to check the type too to avoid processing work_items as issues.
            and self.object_kind == "issue"
            and self.object_attributes.type == "Issue"
            and self.object_attributes.state == "opened"
        )

    async def process_callback(self):
        await sync_to_async(
            address_issue_task.si(
                repo_id=self.project.path_with_namespace,
                issue_iid=self.object_attributes.iid,
                should_reset_plan=self.should_reset_plan(),
            ).delay
        )()

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

        return bool(self._is_quick_action or self._is_merge_request_review or self._is_issue_to_address)

    async def process_callback(self):
        """
        Trigger the task to address the review feedback or issue comment like the plan approval use case.

        GitLab Note Webhook is called multiple times, one per note/discussion.
        """
        if self._is_quick_action:
            logger.info("Found quick action in note: %s", self._quick_action_command.raw)

            # Add a thumbsup emoji to the note to show the user that the quick action will be executed.
            if self._action_scope == Scope.MERGE_REQUEST:
                self._client.create_merge_request_note_emoji(
                    self.project.path_with_namespace, self.merge_request.iid, "thumbsup", self.object_attributes.id
                )
            elif self._action_scope == Scope.ISSUE:
                self._client.create_issue_note_emoji(
                    self.project.path_with_namespace, self.issue.iid, "thumbsup", self.object_attributes.id
                )

            await sync_to_async(
                execute_quick_action_task.si(
                    repo_id=self.project.path_with_namespace,
                    note=self.object_attributes.model_dump(),
                    user=self.user.model_dump(),
                    issue=self.issue and self.issue.model_dump() or None,
                    merge_request=self.merge_request and self.merge_request.model_dump() or None,
                    action_verb=self._quick_action_command.verb,
                    action_args=self._quick_action_command.args,
                    action_scope=self._action_scope,
                ).delay
            )()

        elif self._is_merge_request_review:
            await sync_to_async(
                address_review_task.si(
                    repo_id=self.project.path_with_namespace,
                    merge_request_id=self.merge_request.iid,
                    merge_request_source_branch=self.merge_request.source_branch,
                ).delay
            )()

        elif self._is_issue_to_address:
            await sync_to_async(
                address_issue_task.si(repo_id=self.project.path_with_namespace, issue_iid=self.issue.iid).delay
            )()

    @property
    def _is_quick_action(self) -> bool:
        """
        Accept the webhook if the note is a quick action.
        """
        return bool(
            self.object_attributes.type is None  # Don't accept replies to the quick action note.
            and self._quick_action_command
        )

    @cached_property
    def _is_merge_request_review(self) -> bool:
        """
        Accept the webhook if the note is a merge request comment that mentions DAIV.
        """
        if (
            not self._repo_config.features.auto_address_review_enabled
            or self.object_attributes.noteable_type != NoteableType.MERGE_REQUEST
            or self.object_attributes.action != NoteAction.CREATE
            or not self.merge_request
            or self.merge_request.work_in_progress
            or self.merge_request.state != "opened"
        ):
            return False

        # Shortcut to avoid fetching the discussion if the note mentions DAIV.
        if note_mentions_daiv(self.object_attributes.note, self._client.current_user):
            return True

        # Fetch the discussion to check if it has any notes mentioning DAIV.
        discussion = self._client.get_merge_request_discussion(
            self.project.path_with_namespace, self.merge_request.iid, self.object_attributes.discussion_id
        )
        return discussion_has_daiv_mentions(discussion, self._client.current_user)

    @property
    def _is_issue_to_address(self) -> bool:
        """
        Accept the webhook if the note is a comment for an issue.
        """
        return bool(
            self.object_attributes.noteable_type == NoteableType.ISSUE
            and self.object_attributes.type == "DiscussionNote"  # Only accept replies to the issue discussion.
            and self._repo_config.features.auto_address_issues_enabled
            and self.object_attributes.action == NoteAction.CREATE
            and self.issue
            and self.issue.is_daiv()
            and self.issue.state == "opened"
        )

    @cached_property
    def _quick_action_command(self) -> QuickActionCommand | None:
        """
        Get the quick action command from the note body.
        """
        quick_action_command = parse_quick_action(self.object_attributes.note, self._client.current_user.username)

        if not quick_action_command:
            return None

        action_classes = quick_action_registry.get_actions(verb=quick_action_command.verb, scope=self._action_scope)

        if not action_classes:
            logger.warning(
                "Quick action '%s' not found in registry for scope '%s'", quick_action_command.verb, self._action_scope
            )
            return None

        if len(action_classes) > 1:
            logger.warning(
                "Multiple quick actions found for '%s' in registry for scope '%s': %s",
                quick_action_command.verb,
                self._action_scope,
                [a.verb for a in action_classes],
            )
            return None

        return quick_action_command

    @property
    def _action_scope(self) -> Scope:
        """
        Get the scope of the quick action.
        """
        return Scope.ISSUE if self.object_attributes.noteable_type == NoteableType.ISSUE else Scope.MERGE_REQUEST


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
        return self.ref.endswith(self.project.default_branch) or any(mr.is_daiv() for mr in self.related_merge_requests)

    async def process_callback(self):
        """
        Process the push webhook to update the codebase index and invalidate the cache for the
        repository configurations.
        """
        if self.project.default_branch and self.ref.endswith(self.project.default_branch):
            # Invalidate the cache for the repository configurations, they could have changed.
            RepositoryConfig.invalidate_cache(self.project.path_with_namespace)
            await sync_to_async(
                update_index_repository.si(
                    repo_id=self.project.path_with_namespace, ref=self.project.default_branch
                ).delay
            )()

        for merge_request in self.related_merge_requests:
            if merge_request.is_daiv():
                await sync_to_async(
                    update_index_repository.si(
                        repo_id=self.project.path_with_namespace, ref=merge_request.source_branch
                    ).delay
                )()

    @cached_property
    def related_merge_requests(self) -> list[BaseMergeRequest]:
        """
        Get the merge requests related to the push.
        """
        client = RepoClient.create_instance()
        return client.get_commit_related_merge_requests(self.project.path_with_namespace, commit_sha=self.checkout_sha)
