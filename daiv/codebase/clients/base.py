from __future__ import annotations

import abc
import base64
import functools
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from pydantic import SecretStr

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


def _basic_oauth2(token: str) -> str:
    """Build the ``Basic base64("oauth2:<token>")`` credential DAIV presents for git-over-HTTPS.

    Single source of truth for both transport paths — the egress-proxy-injected header
    (:class:`GitEgressCredential`) and the local-mode clone/subprocess env header
    (:class:`GitAuthEnv`) — so the remote always sees the identical credential shape (the security
    invariant those two carry) rather than two hand-written copies that could drift."""
    encoded = base64.b64encode(f"oauth2:{token}".encode()).decode()
    return f"Basic {encoded}"


@dataclass(frozen=True)
class GitEgressCredential:
    """Egress-proxy contribution for a repo's git platform: which host to allow and the
    ``Authorization`` header to inject so git-over-HTTPS in the sandbox is authenticated.

    The injected header is the sandbox's *only* git credential — the seeded clone carries none
    (see :class:`GitAuthEnv`). ``value`` is ``None`` when no token was provisioned — either the
    platform needs none (e.g. SWE eval's public repos) or one was required but could not be minted.
    The host is still returned so reachability works; auth, if required, then fails at the git
    layer."""

    host: str
    header: str = "Authorization"
    value: SecretStr | None = None

    @classmethod
    def for_token(cls, *, host: str, token: str | None) -> GitEgressCredential:
        """Build a credential injecting ``Authorization: Basic base64("oauth2:<token>")``.
        ``value`` is ``None`` when ``token`` is falsy."""
        return cls(host=host, value=SecretStr(_basic_oauth2(token)) if token else None)


@dataclass(frozen=True)
class GitAuthEnv:
    """A credential + prompt-disabling environment overlay for git-over-HTTPS, carried without the
    token ever touching argv (``ps``-visible) or ``.git/config`` (which is seeded into the sandbox).

    The credential is held in :class:`~pydantic.SecretStr` — like the sibling
    :class:`GitEgressCredential` — so it never appears verbatim in a ``repr``, a log line, or a
    Sentry stack-local; the plaintext is materialised only by :meth:`as_env`, which callers invoke at
    the innermost subprocess/clone boundary. Build via :meth:`for_token`.
    """

    config_key: str
    """The ``http.<origin>.extraheader`` config key. Keeps the clone URL's scheme and port because
    git matches ``http.<url>.*`` by prefix: a mismatch would silently send no credential."""

    header: SecretStr
    """The ``Authorization: Basic base64("oauth2:<token>")`` header value — the same shape the egress
    proxy injects (see :meth:`GitEgressCredential.for_token`)."""

    @classmethod
    def for_token(cls, clone_url: str, token: str) -> GitAuthEnv:
        """Build the overlay for ``clone_url``'s origin authenticated with ``token``.

        Args:
            clone_url: The repository's credential-less HTTP(S) clone URL.
            token: The token to authenticate with.
        """
        parsed = urlparse(clone_url)
        return cls(
            config_key=f"http.{parsed.scheme}://{parsed.netloc}/.extraheader",
            header=SecretStr(f"Authorization: {_basic_oauth2(token)}"),
        )

    def as_env(self) -> dict[str, str]:
        """Materialise the overlay as git subprocess environment variables (plaintext credential).

        ``GIT_CONFIG_{COUNT,KEY_0,VALUE_0}`` apply the command-scoped ``extraheader``.
        ``GIT_TERMINAL_PROMPT=0`` **and** ``GIT_ASKPASS=""`` together disable every prompt path so a
        *rejected* credential fails fast with ``could not read Username`` — one of
        :func:`core.utils.is_git_auth_error_text`'s markers, so the clone-retry self-heal and
        push-failure classifier keep recognising auth errors. Both are needed: with only
        ``GIT_TERMINAL_PROMPT=0`` git still falls back to an inherited ``SSH_ASKPASS`` GUI helper and
        hangs; the empty ``GIT_ASKPASS`` is non-null (short-circuiting that fallback chain) yet empty
        (so nothing is executed), leaving only the disabled terminal prompt.
        """
        return {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": self.config_key,
            "GIT_CONFIG_VALUE_0": self.header.get_secret_value(),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
        }


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
    def list_repositories(
        self, search: str | None = None, topics: list[str] | None = None, limit: int | None = None
    ) -> list[Repository]:
        pass

    @abc.abstractmethod
    def is_branch_protected(self, repo_id: str, branch: str) -> bool:
        """
        Return whether ``branch`` is protected on the remote (covers both exact-name
        and wildcard protection rules — the platform branch resource resolves them).

        Args:
            repo_id: The repository ID.
            branch: The branch name to check.

        Returns:
            ``True`` if the branch exists and is protected, ``False`` otherwise.
        """
        pass

    @abc.abstractmethod
    def list_branches(self, repo_id: str, search: str | None = None, limit: int = 20) -> list[str]:
        """
        Return up to ``limit`` branch names for ``repo_id``.

        If ``search`` is provided, branches are filtered case-insensitively by substring.
        Platforms that support server-side search use it; others filter client-side.
        """
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

    def get_git_egress_credential(self, repository: Repository) -> GitEgressCredential | None:
        """Egress allow-rule + credential for this repo's git platform, or ``None`` for platforms
        that need none. Resolved per run because the host is repo-derived and the token is
        short-lived — never stored on the environment.

        The shared shape lives here — derive the host from the clone URL, then build a
        ``Basic oauth2:<token>`` credential. Platforms supply only the token via
        :meth:`_git_egress_token` (overriding this method is unnecessary)."""
        host = urlparse(repository.clone_url).hostname
        if not host:
            return None
        return GitEgressCredential.for_token(host=host, token=self._git_egress_token(repository))

    def _git_egress_token(self, repository: Repository) -> str | None:
        """Short-lived token authenticating git-over-HTTPS for this repo's platform, or ``None`` for
        platforms that need no credential (host-only reachability). Overridden by GitLab/GitHub."""
        return None

    def get_git_auth_env(self, repository: Repository) -> GitAuthEnv | None:
        """Per-invocation credential overlay for *local-mode* git network operations
        (push/fetch/ls-remote), or ``None`` for platforms whose remotes need no credential
        (e.g. SWE eval's public repos).

        The clone persists no credential (see :class:`GitAuthEnv`), so sandbox-disabled runs must
        overlay it on each git subprocess instead. Resolved at call time — the token is short-lived,
        so it is minted/fetched per publish rather than pinned at clone time. A ``None`` return also
        covers the case where a credential *was* required but could not be minted; the debug log
        distinguishes that from the no-credential-needed case."""
        token = self._git_egress_token(repository)
        if not token:
            logger.debug(
                "No git credential resolved for %s; local-mode git will run unauthenticated "
                "(expected for public-repo platforms, otherwise a token could not be minted)",
                repository.slug,
            )
            return None
        return GitAuthEnv.for_token(repository.clone_url, token)

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
    def get_merge_request_by_branches(
        self, repo_id: str, source_branch: str, target_branch: str
    ) -> MergeRequest | None:
        """
        Return the first open merge request for this source/target branch pair, or ``None``.

        Args:
            repo_id: The repository ID.
            source_branch: The source branch.
            target_branch: The target branch.

        Returns:
            The first open MR matching the branch pair, or ``None`` if none exist.
        """
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
