import logging
from functools import cached_property
from typing import Any, Literal

from asgiref.sync import sync_to_async
from quick_actions.base import Scope
from quick_actions.parser import QuickActionCommand, parse_quick_action
from quick_actions.registry import quick_action_registry
from quick_actions.tasks import execute_quick_action_task

from codebase.api.callbacks import BaseCallback
from codebase.clients import RepoClient
from codebase.repo_config import RepositoryConfig
from codebase.tasks import address_issue_task

from .models import Comment, Issue, IssueChanges, Repository

logger = logging.getLogger("daiv.webhooks")


class IssueCallback(BaseCallback):
    """
    Gitlab Issue Webhook
    """

    action: Literal["opened", "edited", "reopened", "labeled"]
    issue: Issue
    repository: Repository
    changes: IssueChanges | None = None

    def accept_callback(self) -> bool:
        return (
            RepositoryConfig.get_config(self.repository.full_name).issue_addressing.enabled
            and self.issue.is_daiv()
            and self.issue.state == "open"
            and (
                self.action == "edited"
                and bool(self.changes and (self.changes.body.from_value != "" or self.changes.title.from_value != ""))
                or self.action in ["opened", "reopened", "labeled"]
            )
        )

    async def process_callback(self):
        await sync_to_async(
            address_issue_task.si(
                repo_id=self.repository.full_name,
                issue_iid=self.issue.number,
                should_reset_plan=self.should_reset_plan(),
            ).delay
        )()

    def should_reset_plan(self) -> bool:
        """
        Check if the plan should be reset.
        """
        return bool(
            self.action == "edited"
            and bool(self.changes and (self.changes.body.from_value != "" or self.changes.title.from_value != ""))
        )


class IssueCommentCallback(BaseCallback):
    """
    Gitlab Note Webhook
    """

    action: Literal["created", "edited", "deleted"]
    issue: Issue
    repository: Repository
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

        return bool(self._is_quick_action or self._is_issue_to_address)

    async def process_callback(self):
        """
        Trigger the task to address the review feedback or issue comment like the plan approval use case.

        GitLab Note Webhook is called multiple times, one per note/discussion.
        """
        if self._is_quick_action:
            logger.info("Found quick action in note: '%s'", self._quick_action_command.raw)

            self._client.create_issue_note_emoji(self.repository.full_name, self.issue.number, "+1", self.comment.id)

            await sync_to_async(
                execute_quick_action_task.si(
                    repo_id=self.repository.full_name,
                    discussion_id="",  # GitHub doesn't have discussions like GitLab.
                    note_id=self.comment.id,
                    issue_id=self.issue.number,
                    merge_request_id=None,
                    action_verb=self._quick_action_command.verb,
                    action_args=" ".join(self._quick_action_command.args),
                    action_scope=Scope.ISSUE,
                ).delay
            )()

        elif self._is_issue_to_address:
            await sync_to_async(
                address_issue_task.si(repo_id=self.repository.full_name, issue_iid=self.issue.number).delay
            )()

    @property
    def _is_quick_action(self) -> bool:
        """
        Accept the webhook if the note is a quick action.
        """
        return bool(self._repo_config.quick_actions.enabled and self._quick_action_command)

    @property
    def _is_issue_to_address(self) -> bool:
        """
        Accept the webhook if the note is a comment for an issue.
        """
        return bool(
            self._repo_config.issue_addressing.enabled
            and self.issue
            and self.action == "created"
            and self.issue.is_daiv()
            and self.issue.state == "open"
        )

    @cached_property
    def _quick_action_command(self) -> QuickActionCommand | None:
        """
        Get the quick action command from the note body.
        """
        quick_action_command = parse_quick_action(self.comment.body, self._client.current_user.username)

        logger.debug("GitHub quick action command: %s", quick_action_command)

        if not quick_action_command:
            return None

        action_classes = quick_action_registry.get_actions(verb=quick_action_command.verb, scope=Scope.ISSUE)

        if not action_classes:
            logger.warning(
                "Quick action '%s' not found in registry for scope '%s'", quick_action_command.verb, Scope.ISSUE
            )
            return None

        if len(action_classes) > 1:
            logger.warning(
                "Multiple quick actions found for '%s' in registry for scope '%s': %s",
                quick_action_command.verb,
                Scope.ISSUE,
                [a.verb for a in action_classes],
            )
            return None

        return quick_action_command


class PushCallback(BaseCallback):
    """
    GitHub Push Webhook for automatically update the codebase index.
    """

    repository: Repository
    ref: str

    def accept_callback(self) -> bool:
        """
        Accept the webhook if the push is to the default branch or to any branch with MR created.
        """
        return self.ref.endswith(self.repository.default_branch)

    async def process_callback(self):
        """
        Process the push webhook to update the codebase index and invalidate the cache for the
        repository configurations.
        """
        if self.repository.default_branch and self.ref.endswith(self.repository.default_branch):
            # Invalidate the cache for the repository configurations, they could have changed.
            RepositoryConfig.invalidate_cache(self.repository.full_name)
