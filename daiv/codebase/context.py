import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from git import Repo  # noqa: TC002

from codebase.base import GitPlatform, Issue, MergeRequest, Repository, Scope  # noqa: TC001
from codebase.clients import RepoClient
from codebase.exceptions import SingleRepoRequiredError
from codebase.repo_config import RepositoryConfig  # noqa: TC001
from core.sandbox.client import DAIVSandboxClient, reset_run_sandbox_client, set_run_sandbox_client
from core.sandbox.command_policy import SandboxCommandPolicy  # noqa: TC001
from core.sandbox.schemas import EgressConfigRequest  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


logger = logging.getLogger("daiv.codebase")


@dataclass(frozen=True)
class SandboxRuntime:
    """Effective sandbox configuration for the current run.

    Built by :func:`sandbox_envs.services.merge_sandbox_runtime` (invoked from
    :func:`set_runtime_ctx`) from two inputs: the per-run env (either picked
    explicitly via ``sandbox_env_id`` or auto-resolved from the repo via
    :func:`sandbox_envs.services.resolve_env_for_run`) and the GLOBAL default
    env. ``command_policy`` is currently always the empty default; per-env
    policies are a future iteration.
    """

    base_image: str | None
    memory_bytes: int | None
    cpus: float | None
    env_vars: dict[str, str]
    command_policy: SandboxCommandPolicy
    egress: EgressConfigRequest | None = None

    @property
    def enabled(self) -> bool:
        return self.base_image is not None


@dataclass(frozen=True)
class RepoHandle:
    """Bindings for a single repository within a RuntimeCtx.

    A RuntimeCtx holds exactly one of these today (enforced in
    :meth:`RuntimeCtx.__post_init__`). The tuple shape on RuntimeCtx is the
    multi-repo seam; the forwarding properties (``repository``, ``gitrepo``,
    ``git_platform``, ``config``) make single-handle access read like a flat
    dataclass.
    """

    repo_id: str
    git_platform: GitPlatform
    repository: Repository
    gitrepo: Repo
    config: RepositoryConfig


@dataclass(frozen=True)
class RuntimeCtx:
    """Per-run context. Holds a tuple of repository handles plus shared agent-level state.

    The constructor enforces ``len(repos) == 1`` (raising
    :class:`SingleRepoRequiredError` otherwise); forwarding properties
    (``repository``, ``gitrepo``, ``git_platform``, ``config``) delegate to
    ``self.repo``. The tuple is the multi-repo seam for the future, not a
    capability today.
    """

    bot_username: str
    repos: tuple[RepoHandle, ...] = ()
    sandbox: SandboxRuntime | None = None
    """The effective sandbox configuration for the current run"""
    scope: Scope | None = None
    issue: Issue | None = None
    merge_request: MergeRequest | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.repos, tuple):
            object.__setattr__(self, "repos", tuple(self.repos))
        if len(self.repos) != 1:
            raise SingleRepoRequiredError(actual=len(self.repos))

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

    @property
    def config(self) -> RepositoryConfig:
        return self.repo.config


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
    sandbox_env_id: str | None = None,
    **kwargs: Any,
) -> AsyncIterator[RuntimeCtx]:
    """Set the runtime context and load repository files to a temporary directory.

    Args:
        repo_id: The repository identifier
        scope: The scope of the context.
        ref: The reference branch or tag. If None, the default branch will be used.
        issue: The issue object if the context is scoped to an issue.
        merge_request: The merge request object if the context is scoped to a merge request.
        offline: Whether to use the cached configuration or to fetch it from the repository.
        sandbox_env_id: Optional per-run sandbox environment UUID. When provided, the env
            is resolved and merged with the GLOBAL default to build ``ctx.sandbox``.
            When not provided, Auto-resolution selects an env via
            :func:`sandbox_envs.services.resolve_env_for_run` using ``repo_id``; falls back
            to the GLOBAL default env if nothing matches.
        **kwargs: Additional keyword arguments to pass to the repository client.

    Yields:
        RuntimeCtx: The runtime context
    """
    from sandbox_envs.services import (
        augment_sandbox_with_platform_egress,
        get_global_default,
        merge_sandbox_runtime,
        resolve_env_for_run,
        resolve_sandbox_env,
        row_to_override,
    )

    repo_client = RepoClient.create_instance(**kwargs)
    repository = repo_client.get_repository(repo_id)
    config = RepositoryConfig.get_config(repo_id=repo_id, repository=repository, offline=offline)

    if ref is None:
        ref = cast("str", config.default_branch)

    if sandbox_env_id:
        per_run = await resolve_sandbox_env(sandbox_env_id)
    else:
        auto_env = await resolve_env_for_run(user=None, repo_id=repo_id)
        per_run = row_to_override(auto_env) if auto_env is not None else None
    global_default = await get_global_default()
    sandbox = merge_sandbox_runtime(per_run=per_run, global_default=global_default)
    # Always reach + authenticate the repo's git platform for git-over-HTTPS in the sandbox — DAIV
    # pushes from inside the sandbox, so even a network-off env is opened for the platform host when a
    # token can be minted. Runtime-only (never stored on the env); a no-op only when the sandbox is
    # disabled, or when network is off and no platform token is available (e.g. eval runs).
    sandbox = augment_sandbox_with_platform_egress(sandbox, repo_client, repository)

    # Own the sandbox transport for the whole run: one httpx connection pool, injected into the
    # backend + middlewares by create_daiv_agent (and read by the manager recovery path). Opening
    # the client is cheap (httpx connects lazily on first request), so idling through the
    # clone/graph-build phase costs nothing. Gated on `sandbox.enabled` so sandbox-disabled /
    # file-only flows never construct one.
    sandbox_client: DAIVSandboxClient | None = None
    client_token = None
    if sandbox.enabled:
        sandbox_client = DAIVSandboxClient()
        await sandbox_client.open()
        client_token = set_run_sandbox_client(sandbox_client)

    try:
        with repo_client.load_repo(repository, sha=ref) as repo:
            handle = RepoHandle(
                repo_id=repo_id,
                git_platform=repo_client.git_platform,
                repository=repository,
                gitrepo=repo,
                config=config,
            )
            ctx = RuntimeCtx(
                bot_username=repo_client.current_user.username,
                repos=(handle,),
                sandbox=sandbox,
                scope=scope,
                issue=issue,
                merge_request=merge_request,
            )
            token = runtime_ctx.set(ctx)
            try:
                yield ctx
            finally:
                runtime_ctx.reset(token)
    finally:
        if sandbox_client is not None and client_token is not None:
            try:
                await sandbox_client.close()
            except Exception:
                # A transport-level close failure must not mask whatever the run was already raising,
                # and the contextvar reset below must still run so it is never left bound to a closed
                # client. Log and continue.
                logger.exception("Failed to close run-scoped sandbox client")
            finally:
                reset_run_sandbox_client(client_token)


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
