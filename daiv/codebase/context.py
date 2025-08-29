from collections.abc import Iterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from codebase.clients import RepoClient
from codebase.signals import before_reset_repository_ctx
from core.config import RepositoryConfig


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


repository_ctx: ContextVar[RepositoryCtx | None] = ContextVar[RepositoryCtx | None]("repository_ctx", default=None)


@asynccontextmanager
async def set_repository_ctx(repo_id: str, *, ref: str | None = None) -> Iterator[RepositoryCtx]:
    """
    Set the repository context and load repository files to a temporary directory.

    Args:
        repo_id: The repository identifier
        ref: The reference branch or tag. If None, the default branch will be used.

    Yields:
        RepositoryCtx: The repository context
    """
    repo_client = RepoClient.create_instance()

    repository = repo_client.get_repository(repo_id)

    config = RepositoryConfig.get_config(repo_id=repo_id, repository=repository)

    if ref is None:
        ref = cast("str", config.default_branch)

    with repo_client.load_repo(repository, sha=ref) as repo_dir:
        ctx = RepositoryCtx(repo_id=repo_id, ref=ref, repo_dir=repo_dir, config=config)
        token = repository_ctx.set(ctx)
        try:
            yield ctx
        finally:
            await before_reset_repository_ctx.asend(None)
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
