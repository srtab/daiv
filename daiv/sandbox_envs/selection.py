"""Which sandbox environment a run gets.

A trigger picks the environment when the run is requested and records it on the ``Run``; a ``Session`` keeps the
environment of its first run. The run builds its sandbox from that id without re-matching bindings
(:func:`sandbox_envs.services.build_sandbox_spec`). The rule, per repository:

1. An environment the caller names, by id or name (:func:`resolve_env_for_user`); it overrides the rest.
2. The caller's USER environment whose ``repo_ids`` lists the repository. Skipped when ``user`` is ``None``, which
   webhook callbacks pass.
3. A GLOBAL environment whose ``repo_ids`` lists the repository.
4. The GLOBAL default, or ``None`` when there is none.

:func:`resolve_env_for_run` applies steps 2–4 to one repository, and :func:`aresolve_repo_envs` to a batch.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from uuid import UUID

from asgiref.sync import async_to_sync

from sandbox_envs.models import SandboxEnvironment

if TYPE_CHECKING:
    from sessions.services import RepoTarget


@dataclass(frozen=True)
class _Candidates:
    """Every environment steps 2–4 can pick for ``user``, read once."""

    user_envs: list[SandboxEnvironment]
    global_envs: list[SandboxEnvironment]
    global_default: SandboxEnvironment | None

    def match(self, repo_id: str | None) -> SandboxEnvironment | None:
        if repo_id:
            for env in (*self.user_envs, *self.global_envs):
                if repo_id in (env.repo_ids or []):
                    return env
        return self.global_default


async def _acandidates(user) -> _Candidates:
    user_envs: list[SandboxEnvironment] = []
    if user is not None and getattr(user, "is_authenticated", False):
        user_envs = [e async for e in SandboxEnvironment.objects.user_envs(user)]
    global_envs = [e async for e in SandboxEnvironment.objects.global_envs().filter(is_default=False)]
    return _Candidates(user_envs, global_envs, await SandboxEnvironment.objects.aglobal_default())


async def aresolve_repo_envs(*, user, repos: list[RepoTarget], explicit_env_id: str | None) -> list[RepoTarget]:
    """Stamp ``sandbox_environment_id`` on each :class:`sessions.services.RepoTarget`.

    When ``explicit_env_id`` is set every target gets that id; otherwise each repo goes through steps 2–4 of the
    module's rule, matched against a per-call snapshot of USER envs (owned by ``user``), GLOBAL non-default envs,
    and the GLOBAL default.

    One snapshot per call keeps the cost flat regardless of batch size (up to ``MAX_REPOS_PER_BATCH``
    repos), instead of repeating ``resolve_env_for_run``'s up-to-three queries per repo.
    Returns a new list; input is not mutated.
    """
    if explicit_env_id is not None:
        return [replace(t, sandbox_environment_id=explicit_env_id) for t in repos]

    candidates = await _acandidates(user)
    resolved = []
    for t in repos:
        env = candidates.match(t.repo_id)
        resolved.append(replace(t, sandbox_environment_id=str(env.id) if env is not None else None))
    return resolved


def resolve_repo_envs(*, user, repos: list[RepoTarget], explicit_env_id: str | None) -> list[RepoTarget]:
    """Synchronous wrapper around :func:`aresolve_repo_envs` for view-layer callers."""
    return async_to_sync(aresolve_repo_envs)(user=user, repos=repos, explicit_env_id=explicit_env_id)


def _looks_like_uuid(s: str) -> bool:
    try:
        UUID(s)
        return True
    except TypeError, ValueError:
        return False


async def resolve_env_for_user(user, name_or_id: str | None) -> SandboxEnvironment | None:
    """Resolve a caller-visible env (USER-owned by ``user`` or any GLOBAL) by UUID or
    name. Returns ``None`` if ``name_or_id`` is falsy; raises ``LookupError`` if a
    non-empty value doesn't match any visible env."""
    if not name_or_id:
        return None

    qs = SandboxEnvironment.objects.visible_to(user)
    if _looks_like_uuid(name_or_id):
        env = await qs.filter(pk=name_or_id).afirst()
        if env is not None:
            return env
    env = await qs.filter(name=name_or_id).afirst()
    if env is None:
        valid = [n async for n in qs.values_list("name", flat=True)]
        raise LookupError(f"unknown environment '{name_or_id}'; valid: {valid}")
    return env


async def resolve_env_for_run(*, user, repo_id: str | None) -> SandboxEnvironment | None:
    """Steps 2–4 of the module's rule for ``repo_id``: ``None`` only when nothing matches and there is no GLOBAL
    default.

    ``user`` may be ``None`` (webhook callbacks pass it), which skips step 2.
    """
    return (await _acandidates(user)).match(repo_id)
