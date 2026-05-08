from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from git import Repo  # noqa: TC002

from codebase.base import GitPlatform, Issue, MergeRequest, Repository, Scope  # noqa: TC001
from codebase.clients import RepoClient
from codebase.exceptions import SingleRepoRequiredError
from codebase.repo_config import RepositoryConfig

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@dataclass(frozen=True)
class RepoHandle:
    """A single repository's bindings within a RuntimeCtx.

    A RuntimeCtx holds 0..N of these. Repository-coupled middleware and tools
    reach repository state through ``RuntimeCtx.repo`` (single-handle convenience
    accessor) or ``RuntimeCtx.repos`` (the canonical collection).
    """

    repo_id: str
    git_platform: GitPlatform
    repository: Repository
    gitrepo: Repo
    config: RepositoryConfig


@dataclass(frozen=True)
class RuntimeCtx:
    """Per-run context. Holds 0..N repository handles plus shared agent-level state.

    - ``repos == []`` is repoless mode (web/MCP/sandbox-only).
    - ``len(repos) == 1`` is the dominant single-repo mode (today's behavior).
    - ``len(repos) >= 2`` is reserved for future multi-repo support.

    Backward-compatible forwarding properties (``repository``, ``gitrepo``,
    ``git_platform``) delegate to ``self.repo`` so legacy call sites work
    unchanged in single-repo mode and raise :class:`SingleRepoRequiredError`
    otherwise (so a tool selected in error surfaces a clear failure rather
    than corrupting state).
    """

    bot_username: str
    repos: list[RepoHandle] = field(default_factory=list)
    scope: Scope | None = None
    issue: Issue | None = None
    merge_request: MergeRequest | None = None
    config: RepositoryConfig = field(default_factory=RepositoryConfig)

    @property
    def has_repo(self) -> bool:
        return bool(self.repos)

    @property
    def repo(self) -> RepoHandle:
        if len(self.repos) != 1:
            raise SingleRepoRequiredError(actual=len(self.repos))
        return self.repos[0]

    @property
    def repository(self) -> Repository:
        return self.repo.repository

    @property
    def gitrepo(self) -> Repo:
        return self.repo.gitrepo

    @property
    def git_platform(self) -> GitPlatform:
        return self.repo.git_platform


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
        handle = RepoHandle(
            repo_id=repo_id, git_platform=repo_client.git_platform, repository=repository, gitrepo=repo, config=config
        )
        ctx = RuntimeCtx(
            bot_username=repo_client.current_user.username,
            repos=[handle],
            scope=scope,
            issue=issue,
            merge_request=merge_request,
            config=config,
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
