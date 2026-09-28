"""Which sandbox environment a run gets.

A trigger picks the environment once, when the run is requested, and records it on the ``Run`` and its
``Session``. The run then builds its sandbox from that id alone (:func:`sandbox_envs.services.build_sandbox_spec`).
The rule, per repository:

1. An environment the caller names, by id or name (:func:`resolve_env_for_user`); it overrides the rest.
2. The caller's USER environment whose ``repo_ids`` lists the repository. Skipped without a signed-in user, as for
   webhook-triggered runs.
3. A GLOBAL environment whose ``repo_ids`` lists the repository.
4. The GLOBAL default, or ``None`` when there is none.

:func:`resolve_env_for_run` applies steps 2–4 to one repository, and :func:`aresolve_repo_envs` to a batch.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import UUID

from asgiref.sync import async_to_sync

from sandbox_envs.models import SandboxEnvironment, Scope

if TYPE_CHECKING:
    from sessions.services import RepoTarget


async def aresolve_repo_envs(*, user, repos: list[RepoTarget], explicit_env_id: str | None) -> list[RepoTarget]:
    """Stamp ``sandbox_environment_id`` on each :class:`sessions.services.RepoTarget`.

    When ``explicit_env_id`` is set every target gets that id; otherwise each repo goes through steps 2–4 of the
    module's rule, matched against a per-call snapshot of USER envs (owned by ``user``), GLOBAL non-default envs,
    and the GLOBAL default.

    One snapshot per call keeps the cost flat regardless of batch size (max-batch = 20
    repos), instead of repeating ``resolve_env_for_run``'s up-to-three queries per repo.
    Returns a new list; input is not mutated.
    """
    if explicit_env_id is not None:
        return [replace(t, sandbox_environment_id=explicit_env_id) for t in repos]

    user_envs: list[SandboxEnvironment] = []
    if user is not None and getattr(user, "is_authenticated", False):
        user_envs = [e async for e in SandboxEnvironment.objects.filter(scope=Scope.USER, user=user).order_by("name")]
    global_repo_envs = [
        e async for e in SandboxEnvironment.objects.filter(scope=Scope.GLOBAL, is_default=False).order_by("name")
    ]
    global_default = await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL, is_default=True).afirst()

    def _match(repo_id: str | None) -> SandboxEnvironment | None:
        if repo_id:
            for env in user_envs:
                if repo_id in (env.repo_ids or []):
                    return env
            for env in global_repo_envs:
                if repo_id in (env.repo_ids or []):
                    return env
        return global_default

    resolved = []
    for t in repos:
        env = _match(t.repo_id)
        resolved.append(replace(t, sandbox_environment_id=str(env.id) if env is not None else None))
    return resolved


def resolve_repo_envs(*, user, repos: list[RepoTarget], explicit_env_id: str | None) -> list[RepoTarget]:
    """Synchronous wrapper around :func:`aresolve_repo_envs` for view-layer callers."""
    return async_to_sync(aresolve_repo_envs)(user=user, repos=repos, explicit_env_id=explicit_env_id)


def looks_like_uuid(s: str) -> bool:
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
    if looks_like_uuid(name_or_id):
        env = await qs.filter(pk=name_or_id).afirst()
        if env is not None:
            return env
    env = await qs.filter(name=name_or_id).afirst()
    if env is None:
        valid = [n async for n in qs.values_list("name", flat=True)]
        raise LookupError(f"unknown environment '{name_or_id}'; valid: {valid}")
    return env


async def resolve_env_for_run(*, user, repo_id: str | None) -> SandboxEnvironment | None:
    """Steps 2–4 of the module's rule for ``repo_id``: ``None`` only when no env is configured at all.

    ``user`` may be ``None`` (e.g. webhook-triggered runs without a DAIV user), which skips step 2.
    """
    if repo_id:
        if user is not None and getattr(user, "is_authenticated", False):
            async for env in SandboxEnvironment.objects.filter(scope=Scope.USER, user=user).order_by("name"):
                if repo_id in (env.repo_ids or []):
                    return env
        async for env in SandboxEnvironment.objects.filter(scope=Scope.GLOBAL, is_default=False).order_by("name"):
            if repo_id in (env.repo_ids or []):
                return env
    return await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL, is_default=True).afirst()
