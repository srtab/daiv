import logging
import sys
from collections.abc import Awaitable, Callable  # noqa: TC003
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING, Any, cast

from asgiref.sync import sync_to_async
from git import Repo  # noqa: TC002
from sandbox_envs.spec import SandboxSpec  # noqa: TC002

from codebase.base import GitPlatform, Issue, MergeRequest, Repository, Scope  # noqa: TC001
from codebase.clients import RepoClient
from codebase.clients.base import GitEgressCredential  # noqa: TC001
from codebase.exceptions import CloneRefNotFoundError, SingleRepoRequiredError
from codebase.references import ExternalRef, assemble_run_references  # noqa: TC001
from codebase.repo_config import RepositoryConfig  # noqa: TC001
from codebase.utils import get_repo_ref
from core.sandbox.client import DAIVSandboxClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator, Sequence


logger = logging.getLogger("daiv.codebase")


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
    current_ref: str
    """The clone's branch, or its commit sha when HEAD is detached, recorded once at clone time. Unlike ``ref`` it is
    never a tag name. Read this, not the clone's live HEAD, which a sandbox run's git never moves."""
    head_detached: bool
    """Whether the clone's HEAD named no branch (``ref`` was a tag or a commit)."""
    clone_seconds: float
    """How long the worker took to clone the repository; both attempts count when the fallback ran."""


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
    sandbox: SandboxSpec | None = None
    """The environment's resolved sandbox spec; the run's ``SandboxSession`` adds the git-platform rule to its
    egress."""
    sandbox_client: DAIVSandboxClient | None = field(default=None, compare=False, repr=False)
    """The run's sandbox transport: opened by :func:`set_runtime_ctx` for an enabled sandbox and closed with it."""
    credential_source: Callable[[], Awaitable[GitEgressCredential | None]] | None = field(
        default=None, compare=False, repr=False
    )
    """Mints the git platform's egress credential for the run's sandbox session; ``None`` without a sandbox. Each call
    mints anew, so the session gets any token the clone's self-heal re-minted."""
    scope: Scope | None = None
    issue: Issue | None = None
    merge_request: MergeRequest | None = None
    references: tuple[ExternalRef, ...] = ()
    """External work items this run addresses; rendered into the MR description and commit
    trailers by the publisher. Includes the derived platform issue ref for issue-scoped runs."""
    acting_user_id: int | None = None
    """The DAIV user who triggered this run, when known. Selects that user's
    personal MCP servers. ``None`` for webhook-triggered runs (issue/MR labels),
    which load global servers only."""
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


def _credential_source(
    repo_client: RepoClient, repository: Repository
) -> Callable[[], Awaitable[GitEgressCredential | None]]:
    """Mint the repository's git egress credential on each call: a turn can outlive the token minted at its start."""

    async def mint() -> GitEgressCredential | None:
        return await sync_to_async(repo_client.get_git_egress_credential)(repository)

    return mint


@asynccontextmanager
async def set_runtime_ctx(
    repo_id: str,
    *,
    scope: Scope,
    ref: str | None = None,
    issue: Issue | None = None,
    merge_request: MergeRequest | None = None,
    offline: bool = False,
    sandbox_spec: SandboxSpec,
    acting_user_id: int | None = None,
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
        sandbox_spec: The run's sandbox (:func:`sandbox_envs.services.build_sandbox_spec`).
        acting_user_id: DAIV user id that triggered the run; selects their personal MCP servers.
        mcp_overrides: Per-run MCP server selection deviations ({name: "on"|"off"}). ``None`` keeps the default set.
        references: Caller-declared external references, from ``Session.external_refs``.
        fallback_ref_on_missing: When True, a clone that fails because ``ref`` no longer exists on
            the remote (a merged-and-deleted branch) retries on the repository default branch
            instead of raising. ``ctx.repo.ref`` then reflects the branch actually used.
        **kwargs: Additional keyword arguments to pass to the repository client.

    Yields:
        RuntimeCtx: The runtime context
    """
    repo_client = RepoClient.create_instance(**kwargs)
    repository = repo_client.get_repository(repo_id)
    config = RepositoryConfig.get_config(repo_id=repo_id, repository=repository, offline=offline)

    if ref is None:
        ref = cast("str", config.default_branch)

    # One client (one httpx pool) per run, carried on the context; httpx connects lazily, so opening it before the
    # clone is free.
    sandbox_client: DAIVSandboxClient | None = None
    if sandbox_spec.enabled:
        sandbox_client = DAIVSandboxClient()
        await sandbox_client.open()

    clone_started = monotonic()
    try:
        with _load_repo_with_optional_fallback(
            repo_client, repository, ref, cast("str", config.default_branch), fallback_ref_on_missing
        ) as (repo, effective_ref):
            handle = RepoHandle(
                repo_id=repo_id,
                git_platform=repo_client.git_platform,
                repository=repository,
                gitrepo=repo,
                config=config,
                ref=effective_ref,
                current_ref=get_repo_ref(repo),
                head_detached=repo.head.is_detached,
                clone_seconds=monotonic() - clone_started,
            )
            ctx = RuntimeCtx(
                bot_username=repo_client.current_user.username,
                repos=(handle,),
                sandbox=sandbox_spec,
                sandbox_client=sandbox_client,
                credential_source=_credential_source(repo_client, repository) if sandbox_client is not None else None,
                scope=scope,
                issue=issue,
                merge_request=merge_request,
                references=assemble_run_references(
                    references, scope=scope, issue=issue, git_platform=repo_client.git_platform
                ),
                acting_user_id=acting_user_id,
                mcp_overrides=mcp_overrides or {},
            )
            token = runtime_ctx.set(ctx)
            try:
                yield ctx
            finally:
                runtime_ctx.reset(token)
    finally:
        if sandbox_client is not None:
            try:
                await sandbox_client.close()
            except Exception:
                # A close failure must not mask whatever the run was already raising.
                logger.exception("Failed to close run-scoped sandbox client")


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
