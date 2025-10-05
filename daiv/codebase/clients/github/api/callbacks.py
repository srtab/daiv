import logging
from functools import cached_property
from typing import Any, Literal

from asgiref.sync import sync_to_async
from quick_actions.base import Scope
from quick_actions.parser import QuickActionCommand, parse_quick_action
from quick_actions.registry import quick_action_registry
from quick_actions.tasks import execute_quick_action_task

from codebase.api.callbacks import BaseCallback
from codebase.base import NoteType
from codebase.clients import RepoClient
from codebase.clients.base import Emoji
from codebase.repo_config import RepositoryConfig
from codebase.tasks import address_issue_task, address_review_task
from codebase.utils import discussion_has_daiv_mentions, note_mentions_daiv

from .models import Comment, Issue, IssueChanges, PullRequest, Repository, Review

logger = logging.getLogger("daiv.webhooks")


class GitHubCallback(BaseCallback):
    """
    Base class for GitHub callbacks.
    """

    repository: Repository


class IssueCallback(GitHubCallback):
    """
    GitHub Issue Webhook for automatically address the issue.
    """

    action: Literal["opened", "edited", "reopened", "labeled"]
    issue: Issue
    changes: IssueChanges | None = None

    def model_post_init(self, __context: Any):
        self._repo_config = RepositoryConfig.get_config(self.repository.full_name)

    def accept_callback(self) -> bool:
        return (
            self._repo_config.issue_addressing.enabled
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


class IssueCommentCallback(GitHubCallback):
    """
    GitHub Note Webhook for automatically address the review feedback on an pull request or process quick actions.
    """

    action: Literal["created", "edited", "deleted"]
    issue: Issue
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

        return bool(self._is_quick_action or self._is_merge_request_review)

    async def process_callback(self):
        """
        Trigger the task to address the review feedback or issue comment like the plan approval use case.

        GitLab Note Webhook is called multiple times, one per note/discussion.
        """
        if self._is_quick_action:
            logger.info("Found quick action in note: '%s'", self._quick_action_command.raw)

            self._client.create_issue_note_emoji(
                self.repository.full_name, self.issue.number, Emoji.THUMBSUP, self.comment.id
            )

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

        elif self._is_merge_request_review:
            self._client.create_issue_note_emoji(
                self.repository.full_name, self.issue.number, Emoji.THUMBSUP, self.comment.id
            )

            # The webhook doesn't provide the source branch, so we need to fetch it from the merge request.
            merge_request = self._client.get_merge_request(self.repository.full_name, self.issue.number)

            await sync_to_async(
                address_review_task.si(
                    repo_id=self.repository.full_name,
                    merge_request_id=self.issue.number,
                    merge_request_source_branch=merge_request.source_branch,
                ).delay
            )()

    @property
    def _is_quick_action(self) -> bool:
        """
        Accept the webhook if the note is a quick action.
        """
        return bool(self._repo_config.quick_actions.enabled and self._quick_action_command)

    @property
    def _is_merge_request_review(self) -> bool:
        """
        Accept the webhook if the note is a merge request comment that mentions DAIV.
        """
        return bool(
            self._repo_config.code_review.enabled
            and self.issue.is_pull_request()
            and self.issue.state == "open"
            # and not self.issue.draft
            and self.action in ["created", "edited"]
            and note_mentions_daiv(self.comment.body, self._client.current_user)
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


class PullRequestReviewCallback(GitHubCallback):
    """
    GitHub Pull Request Review Webhook for automatically address the review feedback.
    """

    action: Literal["submitted", "edited", "dismissed"]
    pull_request: PullRequest
    review: Review

    def model_post_init(self, __context: Any):
        self._client = RepoClient.create_instance()

    def accept_callback(self) -> bool:
        """
        Check if the webhook is accepted.
        """
        return (
            self.action in ["submitted", "edited"]
            and self.pull_request.state == "open"
            # and not self.pull_request.draft
            # Ignore the DAIV review itself
            and self.review.user.id != self._client.current_user.id
            and (
                discussions := self._client.get_merge_request_discussions(
                    self.repository.full_name, self.pull_request.number, [NoteType.DIFF_NOTE]
                )
            )
            and any(discussion_has_daiv_mentions(discussion, self._client.current_user) for discussion in discussions)
        )

    async def process_callback(self):
        """
        Trigger the task to address the review feedback or issue comment like the plan approval use case.

        GitLab Note Webhook is called multiple times, one per note/discussion.
        """
        await sync_to_async(
            address_review_task.si(
                repo_id=self.repository.full_name,
                merge_request_id=self.pull_request.number,
                merge_request_source_branch=self.pull_request.head.ref,
            ).delay
        )()


class PushCallback(GitHubCallback):
    """
    GitHub Push Webhook for automatically invalidate the cache for the repository configurations.
    """

    ref: str

    def accept_callback(self) -> bool:
        """
        Accept the webhook if the push is to the default branch.
        """
        return self.ref.endswith(self.repository.default_branch)

    async def process_callback(self):
        """
        Process the push webhook to invalidate the cache for the repository configurations.
        """
        if self.repository.default_branch and self.ref.endswith(self.repository.default_branch):
            # Invalidate the cache for the repository configurations, they could have changed.
            RepositoryConfig.invalidate_cache(self.repository.full_name)
