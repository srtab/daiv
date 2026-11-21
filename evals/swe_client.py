from __future__ import annotations

import io
import logging
import re
import tarfile
import tempfile
from contextlib import contextmanager
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

from git import Repo

from codebase.base import (
    GitPlatform,
    Issue,
    MergeRequest,
    MergeRequestCommit,
    MergeRequestDiffStats,
    Note,
    RepoMember,
    Repository,
    User,
)
from codebase.clients import RepoClient
from codebase.clients.utils import safe_slug

if TYPE_CHECKING:
    from collections.abc import Generator

    from codebase.clients.base import CommentThread, Emoji, WebhookSetupResult

logger = logging.getLogger("daiv.clients")


def _exclude_rule(path: str) -> str:
    """A gitignore line matching exactly ``path``, relative to the repository root."""
    return "/" + re.sub(r"([*?[\\])", r"\\\1", path) + "\n"


class SWERepoClient(RepoClient):
    """
    SWE-bench client to interact with public OSS repositories without credentials.

    This client is designed for SWE-bench style evaluations where repositories
    are cloned temporarily and cleaned up after use. It does not support
    issue/MR/CI operations as those are not needed for SWE-bench evaluations.

    ``evals/swebench.py`` hands each run a fresh instance through ``set_runtime_ctx(repo_client=...)``, since the
    loaded clone is per-instance state.
    """

    client: None = None  # No API client needed
    git_platform = GitPlatform.SWE

    def __init__(self, repo_host: str, generated_files: bytes | None = None):
        """
        Initialize the SWE client.

        Args:
            repo_host: The repository host to clone from, e.g. ``github.com``.
            generated_files: A tar of files the clone gets after checkout, such as the version module or the
                extensions its build generates.
        """
        self.repo_host = repo_host
        self.generated_files = generated_files
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
        html_url = f"https://{self.repo_host}/{repo_id}"

        return Repository(
            pk=hash(repo_id) % (2**31),  # Generate a deterministic pseudo-ID
            slug=repo_id,
            name=name,
            clone_url=clone_url,
            html_url=html_url,
            default_branch="main",  # Default assumption, can be overridden
            git_platform=self.git_platform,
            topics=[],
        )

    def list_repositories(
        self, search: str | None = None, topics: list[str] | None = None, limit: int | None = None
    ) -> list[Repository]:
        """
        List repositories is not supported for SWE client.

        Raises:
            NotImplementedError: This operation is not supported.
        """
        raise NotImplementedError("SWERepoClient does not support listing repositories")

    def list_repository_members(self, repo_id: str) -> list[RepoMember]:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support listing repository members")

    def is_branch_protected(self, repo_id: str, branch: str) -> bool:
        """SWE sandbox repos have no remote branch protection."""
        return False

    def list_branches(self, repo_id: str, search: str | None = None, limit: int = 20) -> list[str]:
        """List branches is not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support listing branches")

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

    def set_repository_webhooks(
        self,
        repo_id: str,
        url: str,
        push_events_branch_filter: str | None = None,
        enable_ssl_verification: bool = True,
        secret_token: str | None = None,
        update: bool = False,
    ) -> WebhookSetupResult:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support webhooks")

    @contextmanager
    def load_repo(self, repository: Repository, sha: str) -> Generator[Repo]:
        """
        Fetch the specified commit and its history into a temporary directory, and check it out detached.

        Only that commit's history is fetched, with no branches, remotes or tags, because the upstream fix for the
        issue under evaluation is often already merged: a full clone keeps it reachable from a ref, and with the
        refs removed still as an object ``git fsck --unreachable`` lists, which agents in real eval runs read.

        The ``generated_files`` the client was given are then added to the working tree, and to ``.git/info/exclude``
        so they stay out of the captured patch, as some are files the repository does not ignore.

        Sets _loaded_repo that other methods can use to access the repository.

        Args:
            repository: The repository object.
            sha: The commit SHA or branch name to checkout.

        Yields:
            The repository object, in the temporary directory.
        """
        with tempfile.TemporaryDirectory(prefix=f"{safe_slug(repository.slug)}-{repository.pk}") as tmpdir:
            logger.debug("Cloning repository %s to %s", repository.clone_url, tmpdir)

            repo = Repo.init(Path(tmpdir) / "repo")
            repo.git.fetch("--quiet", "--no-tags", repository.clone_url, sha)
            repo.git.checkout("--detach", "FETCH_HEAD")
            if self.generated_files:
                with tarfile.open(fileobj=io.BytesIO(self.generated_files)) as archive:
                    archive.extractall(repo.working_dir, filter="data")
                    names = archive.getnames()
                exclude = Path(repo.git_dir) / "info" / "exclude"
                exclude.parent.mkdir(exist_ok=True)
                with exclude.open("a") as rules:
                    rules.writelines(_exclude_rule(name) for name in names)

            # Store instance variable for reuse by other methods
            self._loaded_repo = repo

            try:
                yield repo
            finally:
                # Clear instance variable when context exits
                self._loaded_repo = None

    def get_issue(self, repo_id: str, issue_id: int) -> Issue:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support issues")

    def create_issue(self, repo_id: str, title: str, description: str, labels: list[str] | None = None) -> int:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support issue creation")

    def get_issue_note(self, repo_id: str, issue_id: int, note_id: int) -> Note:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support issue comments")

    def create_issue_comment(
        self, repo_id: str, issue_id: int, body: str, reply_to_id: str | None = None
    ) -> str | None:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support issue comments")

    def create_issue_emoji(self, repo_id: str, issue_id: int, emoji: Emoji, note_id: int | None = None):
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support issue emojis")

    def has_issue_reaction(self, repo_id: str, issue_id: int, emoji: Emoji) -> bool:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support issue reactions")

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
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support merge requests")

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
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support merge requests")

    def create_merge_request_comment(
        self, repo_id: str, merge_request_id: int, body: str, reply_to: CommentThread | None = None
    ) -> str | None:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support merge request comments")

    def get_merge_request_diff_stats(self, repo_id: str, merge_request_id: int) -> MergeRequestDiffStats:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support merge request diff stats")

    def get_merge_request_commits(self, repo_id: str, merge_request_id: int) -> list[MergeRequestCommit]:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support merge request commits")

    def get_bot_commit_email(self) -> str:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support bot commit email")

    def get_merge_request(self, repo_id: str, merge_request_id: int) -> MergeRequest:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support merge requests")

    def get_merge_request_by_branches(self, repo_id: str, source_branch: str) -> MergeRequest | None:
        """SWE runs have no merge-request concept: there is never an open MR, so report
        none rather than raising — platform-agnostic callers (e.g. GitMiddleware's pre-run
        MR lookup) then fall through gracefully."""
        return None

    def get_merge_request_note(self, repo_id: str, merge_request_id: int, note_id: int) -> Note:
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support merge request comments")

    def create_merge_request_note_emoji(
        self, repo_id: str, merge_request_id: int, emoji: Emoji, note_id: int, *, on_diff: bool = False
    ):
        """Not supported for SWE client."""
        raise NotImplementedError("SWERepoClient does not support merge request emojis")

    @cached_property
    def current_user(self) -> User:
        """
        Get the current user. For SWE client, returns a dummy user.

        Returns:
            A dummy user object.
        """
        return User(id=0, username="swe-bench", name="SWE Bench")
