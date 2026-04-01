from __future__ import annotations

import abc
import functools
import logging
from contextlib import contextmanager
from enum import StrEnum
from functools import cached_property
from typing import TYPE_CHECKING, Any

from codebase.base import (
    Discussion,
    GitPlatform,
    Issue,
    MergeRequest,
    MergeRequestCommit,
    MergeRequestDiffStats,
    Repository,
    User,
)
from codebase.conf import settings

if TYPE_CHECKING:
    from collections.abc import Iterator

    from git import Repo
    from github import Github
    from gitlab import Gitlab

logger = logging.getLogger("daiv.clients")


class Emoji(StrEnum):
    THUMBSUP = "thumbsup"
    EYES = "eyes"


class WebhookSetupResult(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    SKIPPED = "skipped"


class RepoClient(abc.ABC):
    """
    Abstract class for repository clients.
    """

    client: Github | Gitlab
    git_platform: GitPlatform

    # Repository
    @abc.abstractmethod
    def get_repository(self, repo_id: str) -> Repository:
        pass

    @abc.abstractmethod
    def list_repositories(self, search: str | None = None, topics: list[str] | None = None) -> list[Repository]:
        pass

    @abc.abstractmethod
    def get_repository_file(self, repo_id: str, file_path: str, ref: str) -> str | None:
        pass

    @abc.abstractmethod
    async def get_project_uploaded_file(self, repo_id: str, file_path: str) -> bytes | None:
        pass

    @abc.abstractmethod
    def set_repository_webhooks(
        self,
        repo_id: str,
        url: str,
        push_events_branch_filter: str | None = None,
        enable_ssl_verification: bool = True,
        secret_token: str | None = None,
        update: bool = False,
    ) -> WebhookSetupResult:
        pass

    @abc.abstractmethod
    @contextmanager
    def load_repo(self, repository: Repository, sha: str) -> Iterator[Repo]:
        pass

    # Issue
    @abc.abstractmethod
    def get_issue(self, repo_id: str, issue_id: int) -> Issue:
        pass

    @abc.abstractmethod
    def create_issue(self, repo_id: str, title: str, description: str, labels: list[str] | None = None) -> int:
        """
        Create an issue in a repository.

        Args:
            repo_id: The repository ID.
            title: The issue title.
            description: The issue description.
            labels: Optional list of labels to apply to the issue.

        Returns:
            The created issue IID (GitLab) or number (GitHub).
        """
        pass

    @abc.abstractmethod
    def get_issue_comment(self, repo_id: str, issue_id: int, comment_id: str) -> Discussion:
        pass

    @abc.abstractmethod
    def create_issue_comment(
        self, repo_id: str, issue_id: int, body: str, reply_to_id: str | None = None, as_thread: bool = False
    ) -> str | None:
        pass

    @abc.abstractmethod
    def create_issue_emoji(self, repo_id: str, issue_id: int, emoji: Emoji, note_id: int | None = None):
        pass

    @abc.abstractmethod
    def has_issue_reaction(self, repo_id: str, issue_id: int, emoji: Emoji) -> bool:
        """
        Check if an issue has a specific emoji reaction from the current user.

        Args:
            repo_id: The repository ID.
            issue_id: The issue ID.
            emoji: The emoji to check for.

        Returns:
            True if the issue has the reaction, False otherwise.
        """
        pass

    # Merge request
    @abc.abstractmethod
    def update_or_create_merge_request(
        self,
        repo_id: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        labels: list[str] | None = None,
        assignee_id: str | int | None = None,
        as_draft: bool = False,
    ) -> MergeRequest:
        pass

    @abc.abstractmethod
    def update_merge_request(
        self,
        repo_id: str,
        merge_request_id: int,
        as_draft: bool | None = None,
        title: str | None = None,
        description: str | None = None,
        labels: list[str] | None = None,
        assignee_id: str | int | None = None,
    ) -> MergeRequest:
        pass

    @abc.abstractmethod
    def create_merge_request_comment(
        self,
        repo_id: str,
        merge_request_id: int,
        body: str,
        reply_to_id: str | None = None,
        as_thread: bool = False,
        mark_as_resolved: bool = False,
    ) -> str | None:
        pass

    @abc.abstractmethod
    def get_merge_request_diff_stats(self, repo_id: str, merge_request_id: int) -> MergeRequestDiffStats:
        """
        Get diff statistics for a merge request.

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request IID (GitLab) or number (GitHub).

        Returns:
            The diff statistics (lines added, lines removed, files changed).
        """
        pass

    @abc.abstractmethod
    def get_merge_request_commits(self, repo_id: str, merge_request_id: int) -> list[MergeRequestCommit]:
        """
        Get the pre-squash commit list for a merge request with per-commit stats.

        The platform API retains commits even after the source branch is deleted
        or squash-merged, making this reliable for attribution.

        Note: GitHub limits the commits endpoint to 250 per PR; GitLab results
        are capped at 100 to avoid excessive API usage.

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request IID (GitLab) or number (GitHub).

        Returns:
            List of commits with author email and line stats.
        """
        pass

    @abc.abstractmethod
    def get_bot_commit_email(self) -> str:
        """
        Return the email address DAIV uses when authoring commits.

        Returns:
            The bot's commit email address.
        """
        pass

    @abc.abstractmethod
    def get_merge_request(self, repo_id: str, merge_request_id: int) -> MergeRequest:
        pass

    @abc.abstractmethod
    def get_merge_request_comment(self, repo_id: str, merge_request_id: int, comment_id: str) -> Discussion:
        pass

    @abc.abstractmethod
    def create_merge_request_note_emoji(self, repo_id: str, merge_request_id: int, emoji: Emoji, note_id: int):
        pass

    @abc.abstractmethod
    def mark_merge_request_comment_as_resolved(self, repo_id: str, merge_request_id: int, discussion_id: str):
        pass

    # Implemented only on GitLab for now.
    def create_merge_request_inline_discussion(
        self, repo_id: str, merge_request_id: int, body: str, position: dict[str, Any]
    ) -> str:
        """
        Create an inline diff discussion on a merge request anchored to a specific diff position.

        This is a separate method from `create_merge_request_comment` because the `position` hash
        requires nested key encoding that the python-gitlab CLI does not support.

        Args:
            repo_id: The repository ID.
            merge_request_id: The merge request IID.
            body: The discussion body text.
            position: The diff position dict with keys such as position_type, base_sha, start_sha,
                head_sha, old_path, new_path, old_line, new_line.

        Returns:
            The created discussion ID.

        Raises:
            NotImplementedError: If the platform does not support inline MR diff discussions.
        """
        raise NotImplementedError(f"{self.__class__.__name__} does not support inline MR diff discussions")

    # User
    @abc.abstractmethod
    @cached_property
    def current_user(self) -> User:
        pass

    # Factory
    @staticmethod
    @functools.cache
    def create_instance(*, git_platform: GitPlatform = settings.CLIENT, **kwargs: Any) -> RepoClient:
        """
        Get the repository client based on the configuration.

        Args:
            git_platform: The git platform to use (defaults to the configured client).

        Returns:
            The repository client instance.
        """
        from .github import GitHubClient
        from .github.utils import get_github_integration
        from .gitlab import GitLabClient
        from .swe import SWERepoClient

        if git_platform == GitPlatform.GITLAB:
            assert settings.GITLAB_AUTH_TOKEN is not None, "GitLab auth token is not set"

            return GitLabClient(
                auth_token=settings.GITLAB_AUTH_TOKEN.get_secret_value(),
                url=settings.GITLAB_URL and str(settings.GITLAB_URL) or None,
                **kwargs,
            )

        if git_platform == GitPlatform.GITHUB:
            assert settings.GITHUB_PRIVATE_KEY is not None, "GitHub private key is not set"
            assert settings.GITHUB_APP_ID is not None, "GitHub app ID is not set"
            assert settings.GITHUB_INSTALLATION_ID is not None, "GitHub installation ID is not set"

            return GitHubClient(
                integration=get_github_integration(), installation_id=settings.GITHUB_INSTALLATION_ID, **kwargs
            )

        if git_platform == GitPlatform.SWE:
            return SWERepoClient(**kwargs)

        raise ValueError("Invalid repository client configuration")
