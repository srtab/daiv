from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from codebase.clients import RepoClient
from codebase.repo_config import RepositoryConfig
from codebase.signals import before_reset_runtime_ctx

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@dataclass(frozen=True)
class RuntimeCtx:
    """
    Context to be used across the application layers.
    It needs to be set as early as possible on the request lifecycle or celery task.

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

    merge_request_id: int | None = None
    """The merge request identifier if the context is set for a merge request"""

    bot_username: str | None = None
    """The bot username defined on the repository client"""


runtime_ctx: ContextVar[RuntimeCtx | None] = ContextVar[RuntimeCtx | None]("runtime_ctx", default=None)


@asynccontextmanager
async def set_runtime_ctx(
    repo_id: str, *, ref: str | None = None, merge_request_id: int | None = None
) -> Iterator[RuntimeCtx]:
    """
    Set the runtime context and load repository files to a temporary directory.

    Args:
        repo_id: The repository identifier
        ref: The reference branch or tag. If None, the default branch will be used.
        merge_request_id: The merge request identifier if the context is set for a merge request.
    Yields:
        RuntimeCtx: The runtime context
    """
    repo_client = RepoClient.create_instance()

    repository = repo_client.get_repository(repo_id)

    config = RepositoryConfig.get_config(repo_id=repo_id, repository=repository)

    if ref is None:
        ref = cast("str", config.default_branch)

    with repo_client.load_repo(repository, sha=ref) as repo_dir:
        ctx = RuntimeCtx(
            repo_id=repo_id,
            ref=ref,
            repo_dir=repo_dir,
            config=config,
            merge_request_id=merge_request_id,
            bot_username=repo_client.current_user.username,
        )
        token = runtime_ctx.set(ctx)
        try:
            yield ctx
        finally:
            await before_reset_runtime_ctx.asend_robust("set_runtime_ctx")
            runtime_ctx.reset(token)


@contextmanager
def sync_set_runtime_ctx(repo_id: str, ref: str | None = None, merge_request_id: int | None = None):
    """
    Synchronous facade for set_runtime_ctx so it can be used in Celery tasks.

    Args:
        repo_id: The repository identifier
        ref: The reference branch or tag. If None, the default branch will be used.
        merge_request_id: The merge request identifier if the context is set for a merge request.
    """
    repo_client = RepoClient.create_instance()

    repository = repo_client.get_repository(repo_id)

    config = RepositoryConfig.get_config(repo_id=repo_id, repository=repository)

    if ref is None:
        ref = cast("str", config.default_branch)

    with repo_client.load_repo(repository, sha=ref) as repo_dir:
        ctx = RuntimeCtx(repo_id=repo_id, ref=ref, repo_dir=repo_dir, config=config, merge_request_id=merge_request_id)
        token = runtime_ctx.set(ctx)
        try:
            yield ctx
        finally:
            before_reset_runtime_ctx.send_robust("set_runtime_ctx")
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
            "It needs to be set as early as possible on the request lifecycle or celery task. "
            "Use the `codebase.context.set_runtime_ctx` context manager to set the context."
        )
    return ctx
