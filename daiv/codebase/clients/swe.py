from __future__ import annotations

import logging
import tempfile
from contextlib import contextmanager
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

from git import Repo

from codebase.base import Discussion, GitPlatform, Issue, Job, MergeRequest, Pipeline, Repository, User
from codebase.clients import RepoClient
from codebase.clients.utils import safe_slug

if TYPE_CHECKING:
    from collections.abc import Iterator

    from codebase.clients.base import Emoji

logger = logging.getLogger("daiv.clients")


class SWERepoClient(RepoClient):
    """
    SWE-bench client to interact with public OSS repositories without credentials.

    This client is designed for SWE-bench style evaluations where repositories
    are cloned temporarily and cleaned up after use. It does not support
    issue/MR/CI operations as those are not needed for SWE-bench evaluations.
    """

    client: None = None  # No API client needed
    git_platform = GitPlatform.SWE

    def __init__(self, repo_host: str):
        """
        Initialize the SWE client.

        Args:
            repo_host: The repository host to use for cloning.
        """
        self.repo_host = repo_host
        self._loaded_repo: Repo | None = None

    def get_repository(self, repo_id: str) -> Repository:
        """
        Get a repository object for a public OSS repository.

        Args:
            repo_id: The repository identifier in format "owner/name" (e.g., "psf/requests").

        Returns:
            The repository object.
        """
        if "/" not in repo_id:
            raise ValueError(f"Invalid repo_id format: {repo_id}. Expected format: 'owner/name'")

        owner, name = repo_id.split("/", 1)
        clone_url = f"https://{self.repo_host}/{repo_id}.git"

        return Repository(
            pk=hash(repo_id) % (2**31),  # Generate a deterministic pseudo-ID
            slug=repo_id,
            name=name,
            clone_url=clone_url,
            default_branch="main",  # Default assumption, can be overridden
            git_platform=self.git_platform,
            topics=[],
        )

    def list_repositories(self, search: str | None = None, topics: list[str] | None = None) -> list[Repository]:
        """
        List repositories is not supported for SWE client.

        Raises:
            NotImplementedError: This operation is not supported.
        """
        raise NotImplementedError("SWERepoClient does not support listing repositories")

    def get_repository_file(self, repo_id: str, file_path: str, ref: str) -> str | None:
        """
        Get the content of a file in a repository.

        Requires load_repo to have been called first.

        Args:
            repo_id: The repository identifier.
            file_path: The file path.
            ref: The branch, tag, or commit SHA.

        Returns:
            The content of the file, or None if the file doesn't exist or is binary.

        Raises:
            RuntimeError: If load_repo has not been called.
        """
        if self._loaded_repo is None:
            raise RuntimeError(
                "Repository not loaded. Call load_repo() before using get_repository_file(). "
                "This method requires a repository to be loaded via the load_repo context manager."
            )

        file_path_obj = Path(self._loaded_repo.working_dir) / file_path
        if not file_path_obj.exists():
            return None

        try:
            return file_path_obj.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # File is binary or not UTF-8
            return None

    def get_project_uploaded_file(self, repo_id: str, file_path: str) -> bytes | None:
        """
        Get an uploaded file from a repository.

        Requires load_repo to have been called first.

        Args:
            repo_id: The repository identifier.
            file_path: The file path.

        Returns:
            The file content as bytes, or None if not found.

        Raises:
            RuntimeError: If load_repo has not been called.
        """
        if self._loaded_repo is None:
            raise RuntimeError(
                "Repository not loaded. Call load_repo() before using get_project_uploaded_file(). "
                "This method requires a repository to be loaded via the load_repo context manager."
            )

        file_path_obj = Path(self._loaded_repo.working_dir) / file_path
        if not file_path_obj.exists():
            return None

        try:
            return file_path_obj.read_bytes()
        except Exception as e:
            logger.error("Failed to read file %s: %s", file_path, e)
            return None

    def repository_branch_exists(self, repo_id: str, branch: str) -> bool:
        """
        Check if a branch exists in a repository.

        Requires load_repo to have been called first.

        Args:
            repo_id: The repository identifier.
            branch: The branch name.

        Returns:
            True if the branch exists, False otherwise.

        Raises:
            RuntimeError: If load_repo has not been called.
        """
        if self._loaded_repo is None:
            raise RuntimeError(
                "Repository not loaded. Call load_repo() before using repository_branch_exists(). "
                "This method requires a repository to be loaded via the load_repo context manager."
            )

        try:
            # Try to fetch the branch
            self._loaded_repo.git.fetch("origin", branch)
            return True
        except Exception:
            return False

    @contextmanager
    def load_repo(self, repository: Repository, sha: str) -> Iterator[Repo]:
        """
        Clone a repository to a temporary directory and checkout the specified commit.

        Sets _loaded_repo that other methods can use to access the repository.

        Args:
            repository: The repository object.
            sha: The commit SHA or branch name to checkout.

        Yields:
            The repository object cloned to the temporary directory.
        """
        with tempfile.TemporaryDirectory(prefix=f"{safe_slug(repository.slug)}-{repository.pk}") as tmpdir:
            logger.debug("Cloning repository %s to %s", repository.clone_url, tmpdir)

            clone_dir = Path(tmpdir) / "repo"
            clone_dir.mkdir(parents=True, exist_ok=True)
            # Clone the repository without depth restriction to ensure the specific commit is available
            # For SWE-bench, we often need specific historical commits, so a full clone is necessary
            repo = Repo.clone_from(repository.clone_url, clone_dir)
            # Checkout the specific commit/branch
            repo.git.checkout(sha)

            # Store instance variable for reuse by other methods
            self._loaded_repo = repo

            try:
                yield repo
            finally:
                # Clear instance variable when context exits
                self._loaded_repo = None

    @cached_property
    def current_user(self) -> User:
        """
        Get the current user. For SWE client, returns a dummy user.

        Returns:
            A dummy user object.
        """
        return User(id=0, username="swe-bench", name="SWE Bench")

    # Issue/MR/CI methods - not supported for SWE-bench
    def set_repository_webhooks(
        self,
        repo_id: str,
        url: str,
        push_events_branch_filter: str | None = None,
        enable_ssl_verification: bool = True,
        secret_token: str | None = None,
    ) -> bool:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support webhooks")

    def update_or_create_merge_request(
        self,
        repo_id: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        labels: list[str] | None = None,
        assignee_id: int | None = None,
    ) -> MergeRequest:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support merge requests")

    def create_merge_request_comment(
        self,
        repo_id: str,
        merge_request_id: int,
        body: str,
        reply_to_id: str | None = None,
        as_thread: bool = False,
        mark_as_resolved: bool = False,
    ) -> str | None:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support merge request comments")

    def get_issue(self, repo_id: str, issue_id: int) -> Issue:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support issues")

    def create_issue(self, repo_id: str, title: str, description: str, labels: list[str] | None = None) -> int:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support issue creation")

    def create_issue_comment(
        self, repo_id: str, issue_id: int, body: str, reply_to_id: str | None = None, as_thread: bool = False
    ) -> str | None:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support issue comments")

    def update_issue_comment(
        self, repo_id: str, issue_id: int, comment_id: int, body: str, reply_to_id: str | None = None
    ) -> str | None:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support issue comments")

    def create_issue_emoji(self, repo_id: str, issue_id: int, emoji: Emoji, note_id: int | None = None):
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support issue emojis")

    def has_issue_reaction(self, repo_id: str, issue_id: int, emoji: Emoji) -> bool:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support issue reactions")

    def get_issue_comment(self, repo_id: str, issue_id: int, comment_id: str) -> Discussion:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support issue comments")

    def get_issue_related_merge_requests(
        self, repo_id: str, issue_id: int, assignee_id: int | None = None, label: str | None = None
    ) -> list[MergeRequest]:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support issue-related merge requests")

    def get_merge_request(self, repo_id: str, merge_request_id: int) -> MergeRequest:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support merge requests")

    def get_merge_request_latest_pipelines(self, repo_id: str, merge_request_id: int) -> list[Pipeline]:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support pipelines")

    def get_merge_request_review_comments(self, repo_id: str, merge_request_id: int) -> list[Discussion]:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support merge request review comments")

    def get_merge_request_comments(self, repo_id: str, merge_request_id: int) -> list[Discussion]:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support merge request comments")

    def get_merge_request_comment(self, repo_id: str, merge_request_id: int, comment_id: str) -> Discussion:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support merge request comments")

    def create_merge_request_note_emoji(self, repo_id: str, merge_request_id: int, emoji: Emoji, note_id: int):
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support merge request emojis")

    def mark_merge_request_comment_as_resolved(self, repo_id: str, merge_request_id: int, discussion_id: str):
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support resolving merge request comments")

    def get_job(self, repo_id: str, job_id: int) -> Job:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support jobs")

    def job_log_trace(self, repo_id: str, job_id: int) -> str:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support job logs")
