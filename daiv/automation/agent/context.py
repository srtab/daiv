import asyncio
import logging
import sys
from collections.abc import AsyncGenerator, Awaitable, Generator  # noqa: TC003
from contextlib import ExitStack, asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

from asgiref.sync import sync_to_async
from git import Repo
from sandbox_envs.spec import SandboxSpec  # noqa: TC002

from automation.agent.constants import EXTRA_REPOS_PATH, REPO_PATH
from automation.agent.references import ExternalRef, assemble_run_references  # noqa: TC001
from codebase.base import GitPlatform, Issue, MergeRequest, Repository, Scope  # noqa: TC001
from codebase.clients import RepoClient
from codebase.clients.base import GitEgressCredential  # noqa: TC001
from codebase.clients.checkout import (
    DENY_ALL,
    CheckoutOptions,
    CheckoutReport,
    PublishBlock,
    RunRepoAccess,
    SubmoduleCheckout,
    SubmoduleNote,
)
from codebase.conf import settings as codebase_settings
from codebase.exceptions import CloneRefNotFoundError
from codebase.repo_config import RepositoryConfig
from codebase.utils import get_repo_ref
from core.sandbox.client import DAIVSandboxClient

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


logger = logging.getLogger("daiv.agent")


@runtime_checkable
class CredentialSource(Protocol):
    """Mints every repository's git egress credential, main first and one slot per repository (``None`` where it has
    none). A failed mint raises, unless ``keep_going``: then it is logged and its slot is ``None``."""

    def __call__(self, *, keep_going: bool = False) -> Awaitable[list[GitEgressCredential | None]]: ...


@dataclass(frozen=True)
class ExtraRepo:
    """A repository a session opens beside its main one; an empty ``ref`` means its default branch."""

    repo_id: str
    ref: str = ""


@dataclass(frozen=True)
class SubmoduleRole:
    """What makes a :class:`RepoHandle` a submodule's: where it sits in its root's checkout and whether DAIV publishes
    it. ``path`` and ``parent_path`` are relative to the root's top (``parent_path`` is ``""`` under the root)."""

    path: str
    parent_path: str
    recorded_commit: str
    """The commit the parent's clone records."""
    target_branch: str
    """The branch a new merge request of this submodule targets (research R17)."""
    publish_block: PublishBlock | None = None
    """Why DAIV doesn't publish it, settled across the whole session; ``None`` when it does."""
    published_from: str = ""
    """With ``PUBLISHED_ELSEWHERE``: the workspace path of the checkout that publishes the repository."""
    pinned: str = ""
    """The merged commit its parent records this turn (research R21), or ``""``."""
    note: SubmoduleNote | str = ""


@dataclass(frozen=True)
class RepoHandle:
    """Bindings for a single repository within a RuntimeCtx."""

    repo_id: str
    client: RepoClient = field(compare=False, repr=False)
    """The run's platform client, built or handed in once by :func:`set_runtime_ctx`: the agent's client calls go
    through it rather than through ``RepoClient.create_instance``."""
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
    workspace_path: str = REPO_PATH
    """Where the repository sits in ``/workspace``: the main one at ``/workspace/repo``, an extra one under
    ``/workspace/repos``, a submodule inside its root's."""
    checkout: CheckoutReport = field(default=CheckoutReport.EMPTY, compare=False, repr=False)
    """What completing the clone fetched and left out; empty unless the repository's ``.daiv.yml`` opts in."""
    submodules: tuple[RepoHandle, ...] = field(default=(), compare=False, repr=False)
    """Every checked-out submodule below this repository, nested ones included, in checkout order. Never in
    ``RuntimeCtx.repos``: they travel inside their root's checkout."""
    submodule: SubmoduleRole | None = None
    """Set on a submodule's handle; ``None`` on the session's own repositories."""

    def child_path(self, sub: RepoHandle) -> str:
        """``sub``'s path relative to this repository, for a submodule below it."""
        prefix = f"{self.submodule.path}/" if self.submodule is not None else ""
        return cast("SubmoduleRole", sub.submodule).path.removeprefix(prefix)

    @property
    def direct_submodules(self) -> tuple[RepoHandle, ...]:
        """The submodules whose parent is this repository."""
        own_path = self.submodule.path if self.submodule is not None else ""
        return tuple(sub for sub in self.submodules if cast("SubmoduleRole", sub.submodule).parent_path == own_path)


@dataclass(frozen=True)
class RuntimeCtx:
    """Per-run context. Holds the run's repository handles plus shared agent-level state.

    ``repos[0]`` is the main repository, which drives the run; the rest are the session's extra repositories.
    The forwarding properties (``repository``, ``gitrepo``, ``git_platform``, ``config``) read the main handle.
    """

    bot_username: str
    repos: tuple[RepoHandle, ...] = ()
    sandbox: SandboxSpec | None = None
    """The environment's resolved sandbox spec; the run's ``SandboxSession`` adds the git-platform rule to its
    egress."""
    sandbox_client: DAIVSandboxClient | None = field(default=None, compare=False, repr=False)
    """The run's sandbox transport: opened by :func:`set_runtime_ctx` for an enabled sandbox and closed with it."""
    credential_source: CredentialSource | None = field(default=None, compare=False, repr=False)
    """Mints every repository's git egress credential, main first and one per repository (``None`` where it has none),
    for the run's sandbox session; ``None`` without a sandbox. Each call mints anew, so the session gets any token the
    clone's self-heal re-minted."""
    scope: Scope | None = None
    issue: Issue | None = None
    merge_request: MergeRequest | None = None
    references: tuple[ExternalRef, ...] = ()
    """External work items this run addresses; rendered into the MR description and commit
    trailers by the publisher. Includes the derived platform issue ref for issue-scoped runs."""
    acting_user_id: int | None = None
    """The DAIV user who triggered this run, when known; outside issue-scoped runs, the new merge request's
    assignee. ``None`` for webhook-triggered runs (issue/MR labels)."""

    def __post_init__(self) -> None:
        if not isinstance(self.repos, tuple):
            object.__setattr__(self, "repos", tuple(self.repos))
        if not isinstance(self.references, tuple):
            object.__setattr__(self, "references", tuple(self.references))
        if not self.repos:
            raise ValueError("RuntimeCtx needs at least one repository handle")

    @property
    def repo(self) -> RepoHandle:
        return self.repos[0]

    @property
    def extra_repos(self) -> tuple[RepoHandle, ...]:
        return self.repos[1:]

    @property
    def repository(self) -> Repository:
        return self.repo.repository

    @property
    def gitrepo(self) -> Repo:
        return self.repo.gitrepo

    @property
    def git_platform(self) -> GitPlatform:
        return self.repo.client.git_platform

    @property
    def config(self) -> RepositoryConfig:
        return self.repo.config


runtime_ctx: ContextVar[RuntimeCtx | None] = ContextVar[RuntimeCtx | None]("runtime_ctx", default=None)


@contextmanager
def _load_repo_with_optional_fallback(
    repo_client: RepoClient, repository: Repository, ref: str, default_branch: str, fallback: bool
) -> Generator[tuple[Repo, str]]:
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


def _clone_handle(
    clones: ExitStack,
    repo_client: RepoClient,
    repo_id: str,
    repository: Repository,
    config: RepositoryConfig,
    ref: str,
    *,
    fallback: bool,
    workspace_path: str = REPO_PATH,
) -> RepoHandle:
    """Clone ``repository`` at ``ref`` for as long as ``clones`` stays open, and bind it into a handle."""
    started = monotonic()
    repo, effective_ref = clones.enter_context(
        _load_repo_with_optional_fallback(repo_client, repository, ref, cast("str", config.default_branch), fallback)
    )
    return RepoHandle(
        repo_id=repo_id,
        client=repo_client,
        repository=repository,
        gitrepo=repo,
        config=config,
        ref=effective_ref,
        current_ref=get_repo_ref(repo),
        head_detached=repo.head.is_detached,
        clone_seconds=monotonic() - started,
        workspace_path=workspace_path,
    )


def _clone_extra_handle(
    clones: ExitStack, repo_client: RepoClient, extra: ExtraRepo, *, offline: bool, fallback: bool
) -> RepoHandle:
    """Resolve and clone one of the run's extra repositories, for as long as ``clones`` stays open."""
    repository = repo_client.get_repository(extra.repo_id)
    config = RepositoryConfig.get_config(
        repo_id=extra.repo_id, repository=repository, offline=offline, repo_client=repo_client
    )
    ref = extra.ref or cast("str", config.default_branch)
    return _clone_handle(
        clones,
        repo_client,
        extra.repo_id,
        repository,
        config,
        ref,
        fallback=fallback,
        workspace_path=f"{EXTRA_REPOS_PATH}/{repository.slug}",
    )


def _complete_checkout(
    repo_client: RepoClient, handle: RepoHandle, options: CheckoutOptions, access: RunRepoAccess
) -> CheckoutReport:
    """Complete ``handle``'s clone; an unexpected failure leaves it as cloned, with an empty report."""
    base_branch = cast("str", handle.config.default_branch) if handle.head_detached else handle.current_ref
    try:
        return repo_client.complete_checkout(
            handle.gitrepo, handle.repository, options, access, parent_base_branch=base_branch
        )
    except Exception:
        # The clone itself is intact, so the run goes on without what completing it would have added.
        logger.exception("Could not complete the checkout of %s", handle.repository.slug)
        return CheckoutReport.EMPTY


async def _complete_checkouts(
    repo_client: RepoClient,
    handles: Sequence[RepoHandle],
    *,
    access: RunRepoAccess,
    branches: Mapping[str, Mapping[str, str]],
    pins: Mapping[str, Mapping[str, tuple[str, str]]],
    offline: bool,
) -> list[RepoHandle]:
    """Complete every clone, each in a worker thread and all in parallel, then settle across the session which
    checkout publishes each repository and bind a handle per checked-out submodule."""
    reports = await asyncio.gather(
        *(
            asyncio.to_thread(
                _complete_checkout,
                repo_client,
                handle,
                CheckoutOptions.from_config(
                    handle.config.checkout,
                    codebase_settings.LFS_MAX_CLONE_BYTES,
                    branches=branches.get(handle.repo_id),
                    pins=pins.get(handle.repo_id),
                ),
                access,
            )
            for handle in handles
        )
    )
    handles = [replace(handle, checkout=report) for handle, report in zip(handles, reports, strict=True)]
    roles = _submodule_roles(handles)
    return [
        replace(handle, submodules=await _submodule_handles(repo_client, handle, roles[i], offline=offline))
        if roles[i]
        else handle
        for i, handle in enumerate(handles)
    ]


def _submodule_roles(handles: Sequence[RepoHandle]) -> list[list[tuple[SubmoduleCheckout, SubmoduleRole]]]:
    """Each root's checked-out submodules with their session-wide publish verdict (research R14).

    A repository is published by one checkout only: the session's main or extra one, or else the first checkout of it,
    in session order, that nothing else blocks. A submodule inside a blocked one can't be recorded by a published
    parent, so it is blocked too. The checkout's own verdict (the person can't write) comes last.
    """
    publisher = {handle.repo_id: handle.workspace_path for handle in handles}
    roles: list[list[tuple[SubmoduleCheckout, SubmoduleRole]]] = []
    for handle in handles:
        blocked: dict[str, bool] = {}
        root_roles: list[tuple[SubmoduleCheckout, SubmoduleRole]] = []
        for sub in handle.checkout.checked_out_submodules:
            repo_id = cast("str", sub.repo_id)
            workspace_path = f"{handle.workspace_path}/{sub.path}"
            published_from = ""
            if repo_id in publisher:
                block: PublishBlock | None = PublishBlock.PUBLISHED_ELSEWHERE
                published_from = publisher[repo_id]
            elif blocked.get(sub.parent_path, False):
                block = PublishBlock.PARENT_NOT_PUBLISHED
            else:
                block = sub.publish_block
            if block is None:
                publisher[repo_id] = workspace_path
            blocked[sub.path] = block is not None
            root_roles.append((
                sub,
                SubmoduleRole(
                    path=sub.path,
                    parent_path=sub.parent_path,
                    recorded_commit=sub.commit,
                    target_branch=sub.target_branch,
                    publish_block=block,
                    published_from=published_from,
                    pinned=sub.pinned,
                    note=sub.note,
                ),
            ))
        roles.append(root_roles)
    return roles


async def _submodule_handles(
    repo_client: RepoClient,
    root: RepoHandle,
    roles: Sequence[tuple[SubmoduleCheckout, SubmoduleRole]],
    *,
    offline: bool,
) -> tuple[RepoHandle, ...]:
    """A handle per checked-out submodule of ``root``, in checkout order, each holding the submodules below it."""
    configs = await asyncio.gather(
        *(
            asyncio.to_thread(
                RepositoryConfig.get_config,
                repo_id=cast("str", sub.repo_id),
                repository=sub.repository,
                offline=offline,
                repo_client=repo_client,
            )
            for sub, _ in roles
        )
    )
    top = Path(root.gitrepo.working_dir)
    flat = [
        RepoHandle(
            repo_id=cast("str", sub.repo_id),
            client=repo_client,
            repository=cast("Repository", sub.repository),
            gitrepo=Repo(top / sub.path),
            config=config,
            ref=sub.branch or sub.commit,
            current_ref=sub.branch or sub.commit,
            head_detached=not sub.branch,
            clone_seconds=0.0,
            workspace_path=f"{root.workspace_path}/{sub.path}",
            submodule=role,
        )
        for (sub, role), config in zip(roles, configs, strict=True)
    ]
    paths = [role.path for _, role in roles]
    built: dict[str, RepoHandle] = {}
    for index in sorted(range(len(flat)), key=lambda i: -paths[i].count("/")):
        below = tuple(built[other] for other in paths if other.startswith(f"{paths[index]}/"))
        built[paths[index]] = replace(flat[index], submodules=below)
    return tuple(built[path] for path in paths)


def _credential_source(repo_client: RepoClient, repositories: Sequence[Repository]) -> CredentialSource:
    """Mint each repository's git egress credential, main first, on each call: a turn can outlive the tokens minted
    at its start. A repository without one keeps its ``None`` slot, since position tells the main credential apart."""

    async def mint(*, keep_going: bool = False) -> list[GitEgressCredential | None]:
        credentials: list[GitEgressCredential | None] = []
        for repository in repositories:
            try:
                credentials.append(await sync_to_async(repo_client.get_git_egress_credential)(repository))
            except Exception:
                if not keep_going:
                    raise
                logger.exception("Could not mint the git egress credential of %s", repository.slug)
                credentials.append(None)
        return credentials

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
    references: Sequence[ExternalRef] | None = None,
    fallback_ref_on_missing: bool = False,
    repo_client: RepoClient | None = None,
    extra_repos: Sequence[ExtraRepo] = (),
    access: RunRepoAccess = DENY_ALL,
    submodule_branches: Mapping[str, Mapping[str, str]] | None = None,
    submodule_pins: Mapping[str, Mapping[str, tuple[str, str]]] | None = None,
) -> AsyncGenerator[RuntimeCtx]:
    """Set the runtime context and load repository files to a temporary directory.

    Args:
        repo_id: The repository identifier
        scope: The scope of the context.
        ref: The reference branch or tag. If None, the default branch will be used.
        issue: The issue object if the context is scoped to an issue.
        merge_request: The merge request object if the context is scoped to a merge request.
        offline: Whether to use the cached configuration or to fetch it from the repository.
        sandbox_spec: The run's sandbox (:func:`sandbox_envs.services.build_sandbox_spec`).
        acting_user_id: DAIV user id that triggered the run.
        references: Caller-declared external references, from ``Session.external_refs``.
        fallback_ref_on_missing: When True, a clone that fails because ``ref`` no longer exists on
            the remote (a merged-and-deleted branch) retries on the repository default branch
            instead of raising. ``ctx.repo.ref`` then reflects the branch actually used.
        repo_client: The run's platform client (``ctx.repo.client``); the configured platform's when omitted.
        extra_repos: Repositories cloned in parallel beside the main one, in order; a missing ref falls back alike.
        access: What the run's person may read and write; it decides which submodules are checked out and published.
        submodule_branches: Per repository, each submodule path's working branch from earlier turns (research R19).
        submodule_pins: Per repository, each submodule path's ``(recorded_sha, merged_sha)`` (research R21).

    Yields:
        RuntimeCtx: The runtime context
    """
    if repo_client is None:
        repo_client = RepoClient.create_instance()
    repository = repo_client.get_repository(repo_id)
    config = RepositoryConfig.get_config(
        repo_id=repo_id, repository=repository, offline=offline, repo_client=repo_client
    )

    if ref is None:
        ref = cast("str", config.default_branch)

    # One client (one httpx pool) per run, carried on the context; httpx connects lazily, so opening it before the
    # clone is free.
    sandbox_client: DAIVSandboxClient | None = None
    if sandbox_spec.enabled:
        sandbox_client = DAIVSandboxClient()
        await sandbox_client.open()

    try:
        with ExitStack() as clones:
            handles = [
                _clone_handle(clones, repo_client, repo_id, repository, config, ref, fallback=fallback_ref_on_missing)
            ]
            # return_exceptions: every clone must have entered ``clones`` before a failure unwinds it.
            clone_extra = partial(
                _clone_extra_handle, clones, repo_client, offline=offline, fallback=fallback_ref_on_missing
            )
            extras = await asyncio.gather(
                *(asyncio.to_thread(clone_extra, extra) for extra in extra_repos), return_exceptions=True
            )
            for extra_handle in extras:
                if isinstance(extra_handle, BaseException):
                    raise extra_handle
                handles.append(extra_handle)
            handles = await _complete_checkouts(
                repo_client,
                handles,
                access=access,
                branches=submodule_branches or {},
                pins=submodule_pins or {},
                offline=offline,
            )
            ctx = RuntimeCtx(
                bot_username=repo_client.current_user.username,
                repos=tuple(handles),
                sandbox=sandbox_spec,
                sandbox_client=sandbox_client,
                credential_source=(
                    _credential_source(repo_client, [h.repository for h in handles])
                    if sandbox_client is not None
                    else None
                ),
                scope=scope,
                issue=issue,
                merge_request=merge_request,
                references=assemble_run_references(
                    references, scope=scope, issue=issue, git_platform=repo_client.git_platform
                ),
                acting_user_id=acting_user_id,
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
            "Use the `automation.agent.context.set_runtime_ctx` context manager to set the context."
        )
    return ctx
