from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from codebase.clients import RepoClient
from codebase.repo_config import RepositoryConfig
from codebase.signals import before_reset_repository_ctx


@dataclass(frozen=True)
class RepositoryCtx:
    """
    Context to be used across the application layers.
    It needs to be setted as early as possible on the request lifecycle or celery task.

    With this context, we ensure that application layers that need the repository files can access them without doing
    API calls by accessing the defined `repo_dir` directory, which is a temporary directory with the repository files.

    The context is reset at the end of the request lifecycle or celery task.
    """

    repo_id: str
    """The repository identifier"""

    ref: str
    """The reference branch or tag"""

    repo_dir: Path
    """The temporary directory containing the repository files"""

    config: RepositoryConfig
    """The repository configuration"""

    client: RepoClient
    """The repository client"""


repository_ctx: ContextVar[RepositoryCtx | None] = ContextVar[RepositoryCtx | None]("repository_ctx", default=None)


@asynccontextmanager
async def set_repository_ctx(repo_id: str, client: RepoClient, *, ref: str | None = None) -> Iterator[RepositoryCtx]:
    """
    Set the repository context and load repository files to a temporary directory.

    Args:
        repo_id: The repository identifier
        ref: The reference branch or tag. If None, the default branch will be used.

    Yields:
        RepositoryCtx: The repository context
    """
    repository = client.get_repository(repo_id)

    config = RepositoryConfig.get_config(repo_id=repo_id, repository=repository, client=client)

    if ref is None:
        ref = cast("str", config.default_branch)

    with client.load_repo(repository, sha=ref) as repo_dir:
        ctx = RepositoryCtx(repo_id=repo_id, ref=ref, repo_dir=repo_dir, config=config, client=client)
        token = repository_ctx.set(ctx)
        try:
            yield ctx
        finally:
            await before_reset_repository_ctx.asend_robust("set_repository_ctx")
            repository_ctx.reset(token)


@contextmanager
def sync_set_repository_ctx(repo_id: str, client: RepoClient, *, ref: str | None = None):
    """
    Synchronous facade for set_repository_ctx so it can be used in Celery tasks.
    """
    repository = client.get_repository(repo_id)

    config = RepositoryConfig.get_config(repo_id=repo_id, repository=repository, client=client)

    if ref is None:
        ref = cast("str", config.default_branch)

    with client.load_repo(repository, sha=ref) as repo_dir:
        ctx = RepositoryCtx(repo_id=repo_id, ref=ref, repo_dir=repo_dir, config=config, client=client)
        token = repository_ctx.set(ctx)
        try:
            yield ctx
        finally:
            before_reset_repository_ctx.send_robust("set_repository_ctx")
            repository_ctx.reset(token)


def get_repository_ctx() -> RepositoryCtx:
    """
    Get the repository context.

    Raises:
        RuntimeError: If the repository context is not set.
    """
    ctx = repository_ctx.get()
    if ctx is None:
        raise RuntimeError(
            "Repository context not set. "
            "It needs to be set as early as possible on the request lifecycle or celery task. "
            "Use the `codebase.context.set_repository_ctx` context manager to set the context."
        )
    return ctx
