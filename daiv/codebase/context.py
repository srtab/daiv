import logging
import sys
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from git import Repo  # noqa: TC002

from codebase.base import GitPlatform, Issue, MergeRequest, Repository, Scope  # noqa: TC001
from codebase.clients import RepoClient
from codebase.exceptions import CloneRefNotFoundError, SingleRepoRequiredError
from codebase.references import ExternalRef, assemble_run_references  # noqa: TC001
from codebase.repo_config import RepositoryConfig  # noqa: TC001
from core.sandbox.client import DAIVSandboxClient, reset_run_sandbox_client, set_run_sandbox_client
from core.sandbox.command_policy import SandboxCommandPolicy  # noqa: TC001
from core.sandbox.schemas import EgressConfigRequest  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Sequence


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
    ref: str
    """The branch/ref actually checked out — may differ from the requested ref when a
    vanished branch triggered a fallback to the default branch."""


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
    references: tuple[ExternalRef, ...] = ()
    """External work items this run addresses; rendered into the MR description and commit
    trailers by the publisher. Includes the derived platform issue ref for issue-scoped runs."""
    acting_user_id: int | None = None
    """The DAIV user this run acts for, when known. Selects that user's personal MCP servers and,
    beyond the attached project, the credential the git platform tools act with. Webhook-triggered
    runs carry the user the event resolved to; ``None`` only when no DAIV account could be
    resolved, or for a run with no requesting person at all."""
    acting_platform_uid: str | None = None
    """The platform's own user id for the event that started this run, when it came from a webhook.

    ``resolve_user`` matches on username and email before social uid, so ``acting_user_id`` can
    name an account that never linked this platform identity — fine for choosing an MR assignee,
    not for choosing whose credential to spend. When set, the credential must carry the same uid."""
    mcp_overrides: dict = field(default_factory=dict)
    """Per-run MCP server selection deviations ({name: "on"|"off"}). Empty = pure default set.
    Stamped on the Session at creation and read on every run; ``build_runtime_servers`` applies it."""

    def __post_init__(self) -> None:
        if not isinstance(self.repos, tuple):
            object.__setattr__(self, "repos", tuple(self.repos))
        if not isinstance(self.references, tuple):
            object.__setattr__(self, "references", tuple(self.references))
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


@contextmanager
def _load_repo_with_optional_fallback(
    repo_client: RepoClient, repository: Repository, ref: str, default_branch: str, fallback: bool
) -> Iterator[tuple[Repo, str]]:
    """Clone ``repository`` at ``ref``; on a vanished ref, optionally retry on ``default_branch``.

    Yields ``(repo, effective_ref)``. The clone is acquired inside the ``try/except`` but the
    ``yield`` sits OUTSIDE it, so only a clone-acquisition failure can reach the except — an
    exception raised by the yielded body is thrown back at the ``yield`` (via ``gen.throw()``)
    and unwinds the ``finally`` teardown, never the fallback branch. When ``fallback`` is False,
    or the missing ref already *is* the default branch, the ``CloneRefNotFoundError`` propagates.
    """
    try:
        cm = repo_client.load_repo(repository, sha=ref)
        repo = cm.__enter__()
        effective_ref = ref
    except CloneRefNotFoundError:
        if not fallback or ref == default_branch:
            raise
        logger.warning(
            "Clone of %s failed because ref %r no longer exists on the remote; falling back to the default branch %r.",
            repository.slug,
            ref,
            default_branch,
        )
        cm = repo_client.load_repo(repository, sha=default_branch)
        repo = cm.__enter__()
        effective_ref = default_branch
    try:
        yield repo, effective_ref
    finally:
        cm.__exit__(*sys.exc_info())


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
    acting_user_id: int | None = None,
    acting_platform_uid: str | None = None,
    mcp_overrides: dict | None = None,
    references: Sequence[ExternalRef] | None = None,
    fallback_ref_on_missing: bool = False,
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
        acting_user_id: DAIV user id that triggered the run; selects their personal MCP servers
            and, beyond the attached project, the credential the git platform tools act with.
        acting_platform_uid: The platform's own user id for a webhook-triggered run, used to prove
            the resolved DAIV account really linked that platform identity before spending its
            credential.
        mcp_overrides: Per-run MCP server selection deviations ({name: "on"|"off"}). ``None`` keeps the default set.
        references: Caller-declared external references, from ``Session.external_refs``.
        fallback_ref_on_missing: When True, a clone that fails because ``ref`` no longer exists on
            the remote (a merged-and-deleted branch) retries on the repository default branch
            instead of raising. ``ctx.repo.ref`` then reflects the branch actually used.
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
        with _load_repo_with_optional_fallback(
            repo_client, repository, ref, cast("str", config.default_branch), fallback_ref_on_missing
        ) as (repo, effective_ref):
            # Always reach + authenticate the repo's git platform for git-over-HTTPS in the sandbox — DAIV
            # pushes from inside the sandbox, so even a network-off env is opened for the platform host when
            # a token can be minted. Runtime-only (never stored on the env); a no-op only when the sandbox is
            # disabled, or when network is off and no platform token is available (e.g. eval runs).
            # Resolved AFTER the clone so it sees any token the clone's self-heal re-minted (a pre-clone
            # credential would pin the egress proxy to the stale token the clone just discarded).
            sandbox = augment_sandbox_with_platform_egress(sandbox, repo_client, repository)
            handle = RepoHandle(
                repo_id=repo_id,
                git_platform=repo_client.git_platform,
                repository=repository,
                gitrepo=repo,
                config=config,
                ref=effective_ref,
            )
            ctx = RuntimeCtx(
                bot_username=repo_client.current_user.username,
                repos=(handle,),
                sandbox=sandbox,
                scope=scope,
                issue=issue,
                merge_request=merge_request,
                references=assemble_run_references(
                    references, scope=scope, issue=issue, git_platform=repo_client.git_platform
                ),
                acting_user_id=acting_user_id,
                acting_platform_uid=acting_platform_uid,
                mcp_overrides=mcp_overrides or {},
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
