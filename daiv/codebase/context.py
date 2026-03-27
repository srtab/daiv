from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml
from git import InvalidGitRepositoryError, Repo  # noqa: TC002

from codebase.base import GitPlatform, Issue, MergeRequest, Repository, Scope  # noqa: TC001
from codebase.clients import RepoClient
from codebase.repo_config import CONFIGURATION_FILE_NAME, RepositoryConfig

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger("daiv.core")


@dataclass(frozen=True)
class RuntimeCtx:
    """
    Context to be used across the application layers.
    It needs to be set as early as possible on the request lifecycle or task execution.

    With this context, we ensure that application layers that need the repository files can access them without doing
    API calls by accessing the defined `repo_dir` directory, which is a temporary directory with the repository files.

    The context is reset at the end of the request lifecycle or task execution.
    """

    git_platform: GitPlatform
    """The Git platform"""

    repository: Repository
    """The repository object"""

    gitrepo: Repo
    """The Git repository object"""

    config: RepositoryConfig
    """The repository configuration"""

    scope: Scope | None = None
    """The scope of the context. If None, not running in a specific scope."""

    issue: Issue | None = None
    """The issue object if the context is scoped to an issue, None otherwise"""

    merge_request: MergeRequest | None = None
    """The merge request object if the context is scoped to a merge request, None otherwise"""

    bot_username: str | None = None
    """The bot username defined on the repository client"""


@dataclass(frozen=True)
class LocalRuntimeCtx:
    """
    Context for local/ACP mode without a remote git platform.

    Used when the agent operates on an arbitrary filesystem path (e.g., via ACP)
    rather than a cloned repository managed by a git platform API.
    """

    working_dir: Path
    """The working directory path (the ACP session's cwd)."""

    config: RepositoryConfig
    """Repository config loaded from .daiv.yml or defaults."""

    gitrepo: Repo | None = None
    """Optional git.Repo if cwd is inside a git repository."""

    bot_username: str = "daiv"
    """Bot username for the system prompt."""


AgentCtx = RuntimeCtx | LocalRuntimeCtx
"""Union type for either platform or local runtime contexts."""


def get_working_dir(ctx: AgentCtx) -> Path:
    """Return the agent's working directory for either context type."""
    if isinstance(ctx, LocalRuntimeCtx):
        return ctx.working_dir
    return Path(ctx.gitrepo.working_dir)


def create_local_runtime_ctx(cwd: Path) -> LocalRuntimeCtx:
    """
    Create a LocalRuntimeCtx from a filesystem path.

    Loads RepositoryConfig from .daiv.yml if present, auto-detects git if available.

    Args:
        cwd: The working directory path.

    Returns:
        A LocalRuntimeCtx instance.
    """
    config = _load_local_config(cwd)
    gitrepo = _detect_git_repo(cwd)
    return LocalRuntimeCtx(working_dir=cwd, config=config, gitrepo=gitrepo)


def _load_local_config(cwd: Path) -> RepositoryConfig:
    """Load RepositoryConfig from .daiv.yml in the given directory, or return defaults."""
    config_path = cwd / CONFIGURATION_FILE_NAME
    try:
        with Path(config_path).open() as f:
            return RepositoryConfig.model_validate(yaml.safe_load(f) or {})
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning("Failed to parse %s, using defaults", config_path, exc_info=True)
    return RepositoryConfig()


def _detect_git_repo(cwd: Path) -> Repo | None:
    """Detect a git repository at or above the given directory."""
    try:
        return Repo(cwd, search_parent_directories=True)
    except InvalidGitRepositoryError:
        return None
    except Exception:
        logger.debug("Git detection failed for %s", cwd, exc_info=True)
        return None


runtime_ctx: ContextVar[RuntimeCtx | None] = ContextVar[RuntimeCtx | None]("runtime_ctx", default=None)


@asynccontextmanager
async def set_runtime_ctx(
    repo_id: str,
    *,
    scope: Scope,
    ref: str | None = None,
    issue: Issue | None = None,
    merge_request: MergeRequest | None = None,
    offline: bool = False,
    **kwargs: Any,
) -> AsyncIterator[RuntimeCtx]:
    """
    Set the runtime context and load repository files to a temporary directory.

    Args:
        repo_id: The repository identifier
        scope: The scope of the context.
        ref: The reference branch or tag. If None, the default branch will be used.
        issue: The issue object if the context is scoped to an issue, None otherwise
        merge_request: The merge request object if the context is scoped to a merge request, None otherwise
        offline: Whether to use the cached configuration or to fetch it from the repository.
        **kwargs: Additional keyword arguments to pass to the repository client.

    Yields:
        RuntimeCtx: The runtime context
    """
    repo_client = RepoClient.create_instance(**kwargs)

    repository = repo_client.get_repository(repo_id)

    config = RepositoryConfig.get_config(repo_id=repo_id, repository=repository, offline=offline)

    if ref is None:
        ref = cast("str", config.default_branch)

    with repo_client.load_repo(repository, sha=ref) as repo:
        ctx = RuntimeCtx(
            git_platform=repo_client.git_platform,
            repository=repository,
            gitrepo=repo,
            config=config,
            scope=scope,
            issue=issue,
            merge_request=merge_request,
            bot_username=repo_client.current_user.username,
        )
        token = runtime_ctx.set(ctx)
        try:
            yield ctx
        finally:
            runtime_ctx.reset(token)


def get_runtime_ctx() -> RuntimeCtx:
    """
    Get the runtime context.

    Raises:
        RuntimeError: If the runtime context is not set.
    """
    ctx = runtime_ctx.get()
    if ctx is None:
        raise RuntimeError(
            "Runtime context not set. "
            "It needs to be set as early as possible on the request lifecycle or task execution. "
            "Use the `codebase.context.set_runtime_ctx` context manager to set the context."
        )
    return ctx
