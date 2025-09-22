import logging
from functools import cached_property
from typing import Literal

from asgiref.sync import sync_to_async
from quick_actions.base import Scope
from quick_actions.parser import QuickActionCommand, parse_quick_action
from quick_actions.registry import quick_action_registry
from quick_actions.tasks import execute_quick_action_task

from codebase.api.callbacks import BaseCallback
from codebase.repo_config import RepositoryConfig
from codebase.tasks import address_issue_task, address_review_task
from codebase.utils import discussion_has_daiv_mentions, note_mentions_daiv

from .models import Issue, IssueAction, MergeRequest, Note, NoteableType, NoteAction, Project, User

ISSUE_CHANGE_FIELDS = {"title", "description", "labels", "state_id"}

logger = logging.getLogger("daiv.webhooks")


class GitLabCallback(BaseCallback):
    """
    Gitlab Callback base class
    """

    project: Project


class IssueCallback(GitLabCallback):
    """
    Gitlab Issue Webhook
    """

    object_kind: Literal["issue", "work_item"]
    user: User
    object_attributes: Issue
    changes: dict

    def accept_callback(self) -> bool:
        return (
            self._ctx.config.issue_addressing.enabled
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
                client_type=self._ctx.client.client_slug,
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
            or self.user.id == self._ctx.client.current_user.id
        ):
            return False

        return bool(self._is_quick_action or self._is_merge_request_review or self._is_issue_to_address)

    async def process_callback(self):
        """
        Trigger the task to address the review feedback or issue comment like the plan approval use case.

        GitLab Note Webhook is called multiple times, one per note/discussion.
        """
        if self._is_quick_action:
            logger.info("Found quick action in note: '%s'", self._quick_action_command.raw)

            # Add a thumbsup emoji to the note to show the user that the quick action will be executed.
            if self._action_scope == Scope.MERGE_REQUEST:
                self._ctx.client.create_merge_request_note_emoji(
                    self.project.path_with_namespace, self.merge_request.iid, "thumbsup", self.object_attributes.id
                )
            elif self._action_scope == Scope.ISSUE:
                self._ctx.client.create_issue_note_emoji(
                    self.project.path_with_namespace, self.issue.iid, "thumbsup", self.object_attributes.id
                )

            await sync_to_async(
                execute_quick_action_task.si(
                    repo_id=self.project.path_with_namespace,
                    discussion_id=self.object_attributes.discussion_id,
                    note_id=self.object_attributes.id,
                    issue_id=self.issue and self.issue.iid or None,
                    merge_request_id=self.merge_request and self.merge_request.iid or None,
                    action_verb=self._quick_action_command.verb,
                    action_args=" ".join(self._quick_action_command.args),
                    action_scope=self._action_scope,
                    client_type=self._ctx.client.client_slug,
                ).delay
            )()

        elif self._is_merge_request_review:
            self._ctx.client.create_merge_request_note_emoji(
                self.project.path_with_namespace, self.merge_request.iid, "thumbsup", self.object_attributes.id
            )

            await sync_to_async(
                address_review_task.si(
                    repo_id=self.project.path_with_namespace,
                    merge_request_id=self.merge_request.iid,
                    merge_request_source_branch=self.merge_request.source_branch,
                    client_type=self._ctx.client.client_slug,
                ).delay
            )()

        elif self._is_issue_to_address:
            await sync_to_async(
                address_issue_task.si(
                    repo_id=self.project.path_with_namespace,
                    issue_iid=self.issue.iid,
                    client_type=self._ctx.client.client_slug,
                ).delay
            )()

    @property
    def _is_quick_action(self) -> bool:
        """
        Accept the webhook if the note is a quick action.
        """
        return bool(self._ctx.config.quick_actions.enabled and self._quick_action_command)

    @cached_property
    def _is_merge_request_review(self) -> bool:
        """
        Accept the webhook if the note is a merge request comment that mentions DAIV.
        """
        if (
            not self._ctx.config.code_review.enabled
            or self.object_attributes.noteable_type != NoteableType.MERGE_REQUEST
            or self.object_attributes.action != NoteAction.CREATE
            or not self.merge_request
            or self.merge_request.work_in_progress
            or self.merge_request.state != "opened"
        ):
            return False

        # Shortcut to avoid fetching the discussion if the note mentions DAIV.
        if note_mentions_daiv(self.object_attributes.note, self._ctx.client.current_user):
            return True

        # Fetch the discussion to check if it has any notes mentioning DAIV.
        discussion = self._ctx.client.get_merge_request_discussion(
            self.project.path_with_namespace, self.merge_request.iid, self.object_attributes.discussion_id
        )
        return discussion_has_daiv_mentions(discussion, self._ctx.client.current_user)

    @property
    def _is_issue_to_address(self) -> bool:
        """
        Accept the webhook if the note is a comment for an issue.
        """
        return bool(
            self.object_attributes.noteable_type == NoteableType.ISSUE
            and self.object_attributes.type == "DiscussionNote"  # Only accept replies to the issue discussion.
            and self._ctx.config.issue_addressing.enabled
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
        quick_action_command = parse_quick_action(self.object_attributes.note, self._ctx.client.current_user.username)

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


class PushCallback(GitLabCallback):
    """
    Gitlab Push Webhook for automatically update the codebase index.
    """

    object_kind: Literal["push"]
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
