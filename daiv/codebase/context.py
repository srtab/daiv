from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from git import Repo  # noqa: TC002

from codebase.base import GitPlatform, Issue, MergeRequest, Scope  # noqa: TC001
from codebase.clients import RepoClient
from codebase.repo_config import RepositoryConfig

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


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

    repo_id: str
    """The repository identifier"""

    repo: Repo
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
            repo_id=repo_id,
            repo=repo,
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
