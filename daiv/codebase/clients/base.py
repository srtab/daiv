from __future__ import annotations

import abc
import functools
import logging
from enum import StrEnum
from functools import cached_property
from typing import TYPE_CHECKING, Any

from codebase.base import ClientType, Discussion, Issue, Job, MergeRequest, Pipeline, Repository, User
from codebase.conf import settings

if TYPE_CHECKING:
    from collections.abc import Iterator

    from git import Repo
    from github import Github
    from gitlab import Gitlab

logger = logging.getLogger("daiv.clients")


class Emoji(StrEnum):
    THUMBSUP = "thumbsup"


class RepoClient(abc.ABC):
    """
    Abstract class for repository clients.
    """

    client: Github | Gitlab
    client_slug: ClientType

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
    def get_project_uploaded_file(self, repo_id: str, file_path: str) -> bytes | None:
        pass

    @abc.abstractmethod
    def repository_branch_exists(self, repo_id: str, branch: str) -> bool:
        pass

    @abc.abstractmethod
    def set_repository_webhooks(
        self,
        repo_id: str,
        url: str,
        push_events_branch_filter: str | None = None,
        enable_ssl_verification: bool = True,
        secret_token: str | None = None,
    ) -> bool:
        pass

    @abc.abstractmethod
    def update_or_create_merge_request(
        self,
        repo_id: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        labels: list[str] | None = None,
        assignee_id: int | None = None,
    ) -> int | str | None:
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
    def load_repo(self, repository: Repository, sha: str) -> Iterator[Repo]:
        pass

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
    def create_issue_comment(
        self, repo_id: str, issue_id: int, body: str, reply_to_id: str | None = None, as_thread: bool = False
    ) -> str | None:
        pass

    @abc.abstractmethod
    def update_issue_comment(
        self, repo_id: str, issue_id: int, comment_id: int, body: str, reply_to_id: str | None = None
    ) -> str | None:
        pass

    @abc.abstractmethod
    def create_issue_note_emoji(self, repo_id: str, issue_id: int, emoji: Emoji, note_id: str):
        pass

    @abc.abstractmethod
    def get_issue_comment(self, repo_id: str, issue_id: int, comment_id: str) -> Discussion:
        pass

    @abc.abstractmethod
    def get_issue_related_merge_requests(
        self, repo_id: str, issue_id: int, assignee_id: int | None = None, label: str | None = None
    ) -> list[MergeRequest]:
        pass

    @abc.abstractmethod
    @cached_property
    def current_user(self) -> User:
        pass

    @abc.abstractmethod
    def get_merge_request(self, repo_id: str, merge_request_id: int) -> MergeRequest:
        pass

    @abc.abstractmethod
    def get_merge_request_latest_pipelines(self, repo_id: str, merge_request_id: int) -> list[Pipeline]:
        pass

    @abc.abstractmethod
    def get_merge_request_review_comments(self, repo_id: str, merge_request_id: int) -> list[Discussion]:
        pass

    @abc.abstractmethod
    def get_merge_request_comments(self, repo_id: str, merge_request_id: int) -> list[Discussion]:
        pass

    @abc.abstractmethod
    def get_merge_request_comment(self, repo_id: str, merge_request_id: int, comment_id: str) -> Discussion:
        pass

    @abc.abstractmethod
    def create_merge_request_note_emoji(self, repo_id: str, merge_request_id: int, emoji: Emoji, note_id: str):
        pass

    @abc.abstractmethod
    def mark_merge_request_comment_as_resolved(self, repo_id: str, merge_request_id: int, discussion_id: str):
        pass

    @abc.abstractmethod
    def get_job(self, repo_id: str, job_id: int) -> Job:
        pass

    @abc.abstractmethod
    def job_log_trace(self, repo_id: str, job_id: int) -> str:
        pass

    @staticmethod
    @functools.cache
    def create_instance(*, client_slug: ClientType = settings.CLIENT, **kwargs: Any) -> RepoClient:
        """
        Get the repository client based on the configuration.

        Args:
            client_slug: The client slug to use.

        Returns:
            The repository client instance.
        """
        from .github import GitHubClient
        from .gitlab import GitLabClient
        from .swe import SWERepoClient

        if client_slug == ClientType.GITLAB:
            assert settings.GITLAB_AUTH_TOKEN is not None, "GitLab auth token is not set"

            return GitLabClient(
                auth_token=settings.GITLAB_AUTH_TOKEN.get_secret_value(),
                url=settings.GITLAB_URL and str(settings.GITLAB_URL) or None,
                **kwargs,
            )

        if client_slug == ClientType.GITHUB:
            assert settings.GITHUB_PRIVATE_KEY is not None, "GitHub private key is not set"
            assert settings.GITHUB_APP_ID is not None, "GitHub app ID is not set"
            assert settings.GITHUB_INSTALLATION_ID is not None, "GitHub installation ID is not set"

            return GitHubClient(
                private_key=settings.GITHUB_PRIVATE_KEY.get_secret_value(),
                app_id=settings.GITHUB_APP_ID,
                installation_id=settings.GITHUB_INSTALLATION_ID,
                url=settings.GITHUB_URL and str(settings.GITHUB_URL) or None,
                **kwargs,
            )

        if client_slug == ClientType.SWE:
            return SWERepoClient(**kwargs)

        raise ValueError("Invalid repository client configuration")
