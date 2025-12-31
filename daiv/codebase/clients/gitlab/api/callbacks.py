import logging
from functools import cached_property
from typing import Any, Literal

from asgiref.sync import sync_to_async

from codebase.api.callbacks import BaseCallback
from codebase.base import NoteType
from codebase.clients import RepoClient
from codebase.clients.base import Emoji
from codebase.repo_config import RepositoryConfig
from codebase.tasks import address_issue_task, address_mr_comments_task, address_mr_review_task
from codebase.utils import note_mentions_daiv
from quick_actions.base import Scope
from quick_actions.parser import QuickActionCommand, parse_quick_action
from quick_actions.registry import quick_action_registry
from quick_actions.tasks import execute_issue_task, execute_merge_request_task

from .models import Issue, MergeRequest, Note, NoteableType, NoteAction, Project, User

logger = logging.getLogger("daiv.webhooks")


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

        return bool(self._is_quick_action or self._is_issue_comment or self._is_merge_request_review)

    async def process_callback(self):
        """
        Trigger the task to address the review feedback, issue comment or quick action.

        GitLab Note Webhook is called multiple times, one per note/discussion.
        """
        if self._is_quick_action:
            logger.info("Found quick action in note: '%s'", self._quick_action_command.raw)

            # Add a thumbsup emoji to the note to show the user that the quick action will be executed.
            if self._action_scope == Scope.MERGE_REQUEST:
                self._client.create_merge_request_note_emoji(
                    self.project.path_with_namespace, self.merge_request.iid, Emoji.THUMBSUP, self.object_attributes.id
                )
                await sync_to_async(
                    execute_merge_request_task.si(
                        repo_id=self.project.path_with_namespace,
                        comment_id=self.object_attributes.discussion_id,
                        action_command=self._quick_action_command.command,
                        action_args=" ".join(self._quick_action_command.args),
                        merge_request_id=self.merge_request.iid,
                    ).delay
                )()
            elif self._action_scope == Scope.ISSUE:
                self._client.create_issue_note_emoji(
                    self.project.path_with_namespace, self.issue.iid, Emoji.THUMBSUP, self.object_attributes.id
                )
                await sync_to_async(
                    execute_issue_task.si(
                        repo_id=self.project.path_with_namespace,
                        comment_id=self.object_attributes.discussion_id,
                        action_command=self._quick_action_command.command,
                        action_args=" ".join(self._quick_action_command.args),
                        issue_id=self.issue.iid,
                    ).delay
                )()

        elif self._is_issue_comment:
            await sync_to_async(
                address_issue_task.si(
                    repo_id=self.project.path_with_namespace,
                    issue_iid=self.issue.iid,
                    mention_comment_id=self.object_attributes.discussion_id,
                ).delay
            )()

        elif self._is_merge_request_review:
            if self.object_attributes.type in [NoteType.DIFF_NOTE, NoteType.DISCUSSION_NOTE]:
                await sync_to_async(
                    address_mr_review_task.si(
                        repo_id=self.project.path_with_namespace,
                        merge_request_id=self.merge_request.iid,
                        merge_request_source_branch=self.merge_request.source_branch,
                    ).delay
                )()
            elif self.object_attributes.type is None:  # This is a comment note.
                await sync_to_async(
                    address_mr_comments_task.si(
                        repo_id=self.project.path_with_namespace,
                        merge_request_id=self.merge_request.iid,
                        merge_request_source_branch=self.merge_request.source_branch,
                    ).delay
                )()
            else:
                logger.warning("Unsupported note type: %s", self.object_attributes.type)

    @property
    def _is_quick_action(self) -> bool:
        """
        Accept the webhook if the note is a quick action.
        """
        return bool(self._repo_config.quick_actions.enabled and self._quick_action_command)

    @cached_property
    def _is_merge_request_review(self) -> bool:
        """
        Accept the webhook if the note is a merge request comment that mentions DAIV.
        """
        if (
            not self._repo_config.code_review.enabled
            or self.object_attributes.noteable_type != NoteableType.MERGE_REQUEST
            or self.object_attributes.action not in [NoteAction.CREATE, NoteAction.UPDATE]
            or not self.merge_request
            or self.merge_request.state != "opened"
        ):
            return False

        return note_mentions_daiv(self.object_attributes.note, self._client.current_user)

    @cached_property
    def _is_issue_comment(self) -> bool:
        """
        Accept the webhook if the note is an issue comment that mentions DAIV.
        """
        return (
            self.object_attributes.noteable_type == NoteableType.ISSUE
            and self.object_attributes.action == NoteAction.CREATE
            and self.issue
            and self.issue.state == "opened"
            and note_mentions_daiv(self.object_attributes.note, self._client.current_user)
        )

    @cached_property
    def _quick_action_command(self) -> QuickActionCommand | None:
        """
        Get the quick action command from the note body.
        """
        quick_action_command = parse_quick_action(self.object_attributes.note, self._client.current_user.username)

        if not quick_action_command:
            return None

        action_classes = quick_action_registry.get_actions(
            command=quick_action_command.command, scope=self._action_scope
        )

        if not action_classes:
            logger.warning(
                "Quick action '%s' not found in registry for scope '%s'",
                quick_action_command.command,
                self._action_scope,
            )
            return None

        if len(action_classes) > 1:
            logger.warning(
                "Multiple quick actions found for '%s' in registry for scope '%s': %s",
                quick_action_command.command,
                self._action_scope,
                [a.command for a in action_classes],
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
        return self.ref.endswith(self.project.default_branch)

    async def process_callback(self):
        """
        Process the push webhook to update the codebase index and invalidate the cache for the
        repository configurations.
        """
        if self.project.default_branch and self.ref.endswith(self.project.default_branch):
            # Invalidate the cache for the repository configurations, they could have changed.
            RepositoryConfig.invalidate_cache(self.project.path_with_namespace)
