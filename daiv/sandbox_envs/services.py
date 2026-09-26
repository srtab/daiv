from __future__ import annotations

import logging
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING, Literal
from uuid import UUID

from django.db.models import Q

from asgiref.sync import async_to_sync

from sandbox_envs.models import SandboxEnvironment, Scope, _fmt_cpus, _fmt_memory
from sandbox_envs.spec import SandboxEnvOverride

if TYPE_CHECKING:
    from sessions.services import RepoTarget

logger = logging.getLogger("daiv.sandbox_envs")


def row_to_override(env: SandboxEnvironment) -> SandboxEnvOverride:
    from pydantic import ValidationError as PydanticValidationError

    from core.encryption import DecryptionError
    from core.sandbox.schemas import EgressConfigRequest

    try:
        env_vars_rows = env.env_vars or []
    except DecryptionError:
        # Agent run paths must not crash on a key rotation; drop env vars and
        # keep going. The descriptor already logs at exception level.
        logger.error("env_vars decryption failed for SandboxEnvironment id=%s; dropping env_vars", env.id)
        env_vars_rows = []

    egress = None
    if env.is_networked:
        try:
            egress = EgressConfigRequest.from_stored(env.egress_policy, env.egress_secrets or {})
        except DecryptionError, PydanticValidationError, TypeError, ValueError:
            # The env intended restricted egress but its config is unusable (e.g. a rotated
            # DAIV_ENCRYPTION_KEY left the secrets undecryptable, or the row was hand-edited).
            # Fail closed to NO network (egress=None → network_mode=none) — the old deny-all-with-plumbing
            # state no longer exists and would be rejected by the sandbox. Never reach the sidecar with a
            # half/invalid config. logger.exception (not error) so the shape errors — which, unlike
            # DecryptionError, nothing else logs — don't vanish into this branch untraced.
            logger.exception(
                "egress config unusable for SandboxEnvironment id=%s (%s); failing closed to no-network",
                env.id,
                env.name,
            )
            egress = None

    return SandboxEnvOverride(
        base_image=env.base_image or None,
        memory_bytes=env.memory_bytes,
        cpus=float(env.cpus) if isinstance(env.cpus, Decimal) else env.cpus,
        env_vars={
            entry["name"]: entry["value"]
            for entry in env_vars_rows
            if entry.get("name") and entry.get("value") is not None
        },
        egress=egress,
    )


async def resolve_sandbox_env(env_id: str | None) -> SandboxEnvOverride | None:
    """Load the explicit per-run env.

    Returns ``None`` only when no env was requested (``env_id`` is falsy).
    Raises :class:`LookupError` when a non-empty ``env_id`` cannot be resolved
    (malformed UUID or no matching row) — distinguishing this from "no env
    requested" prevents silently masquerading the GLOBAL default as the
    caller-selected env.
    """
    if not env_id:
        return None
    try:
        UUID(env_id)
    except (TypeError, ValueError) as err:
        raise LookupError(f"Malformed sandbox environment id '{env_id}'") from err
    env = await SandboxEnvironment.objects.filter(pk=env_id).afirst()
    if env is None:
        raise LookupError(f"Sandbox environment '{env_id}' not found")
    return row_to_override(env)


async def get_global_default() -> SandboxEnvOverride | None:
    """Resolved GLOBAL default — straight from the row. Returns ``None`` when
    no GLOBAL default row exists."""
    row = await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL, is_default=True).afirst()
    return row_to_override(row) if row is not None else None


def humanise_global_default() -> dict[str, str | bool]:
    """Synchronous, template-friendly view of the GLOBAL default's row values.

    Network is intentionally omitted: the form's Network control is a self-contained On/Off that
    neither displays nor inherits the global default, so only memory/cpus are surfaced here."""
    row = SandboxEnvironment.objects.filter(scope=Scope.GLOBAL, is_default=True).first()
    if row is None:
        return {"memory": "", "cpus": "", "has_memory": False, "has_cpus": False}
    return {
        "memory": _fmt_memory(row.memory_bytes) if row.memory_bytes is not None else "",
        "cpus": _fmt_cpus(row.cpus) if row.cpus is not None else "",
        "has_memory": row.memory_bytes is not None,
        "has_cpus": row.cpus is not None,
    }


def env_picker_context(form) -> dict:
    """Build the picker's ``sandbox_envs`` / ``selected_sandbox_env_id`` context from a form.
    Empty values when the form lacks ``sandbox_environment`` so the partial renders an
    empty popover with only the Auto row."""
    if "sandbox_environment" not in form.fields:
        return {"sandbox_envs": [], "selected_sandbox_env_id": ""}
    bound = form["sandbox_environment"]
    return {"sandbox_envs": list(bound.field.queryset), "selected_sandbox_env_id": str(bound.value() or "")}


async def aresolve_repo_envs(*, user, repos: list[RepoTarget], explicit_env_id: str | None) -> list[RepoTarget]:
    """Stamp ``sandbox_environment_id`` on each :class:`sessions.services.RepoTarget`.

    When ``explicit_env_id`` is set every target gets that id; otherwise each repo is
    matched against a per-call snapshot of USER envs (owned by ``user``), GLOBAL non-default
    envs, and the GLOBAL default, preserving the precedence in :func:`resolve_env_for_run`.

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


async def alist_visible_environments(
    user, *, limit: int | None = None, after: tuple[str, str, UUID] | None = None
) -> list[SandboxEnvironment]:
    """Return the environments visible to ``user`` (own USER envs plus all GLOBAL envs),
    ordered by ``(scope, name, id)``.

    ``after`` is a keyset cursor ``(scope, name, id)`` of the last row already seen; only
    rows strictly after it in that ordering are returned. The ``id`` tie-break keeps the
    cursor unambiguous when two envs share a ``(scope, name)``. ``limit=None`` returns all.
    """
    qs = SandboxEnvironment.objects.visible_to(user).order_by("scope", "name", "id")
    if after is not None:
        after_scope, after_name, after_id = after
        qs = qs.filter(
            Q(scope__gt=after_scope)
            | Q(scope=after_scope, name__gt=after_name)
            | Q(scope=after_scope, name=after_name, id__gt=after_id)
        )
    if limit is not None:
        qs = qs[:limit]
    return [env async for env in qs]


async def resolve_env_for_run(*, user, repo_id: str | None) -> SandboxEnvironment | None:
    """Auto-resolve the SandboxEnvironment to use for a run.

    Resolution chain (per repo):
      1. USER env owned by ``user`` whose ``repo_ids`` contains ``repo_id``.
      2. GLOBAL env whose ``repo_ids`` contains ``repo_id``.
      3. The single GLOBAL env marked ``is_default=True``.
      4. ``None`` when no env is configured at all.

    ``user`` may be ``None`` (e.g. webhook-triggered runs without a DAIV user);
    in that case L1 is skipped.
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


def build_env_trigger(env: SandboxEnvironment, action: Literal["created", "updated", "deleted"]) -> dict:
    """Return the ``{event_name: payload}`` dict for an ``HX-Trigger`` JSON header."""
    return {
        f"env-{action}": {
            "id": str(env.id),
            "name": env.name,
            "scope": env.scope,
            "scope_display": env.get_scope_display(),
            "is_default": env.is_default,
            "summary": env.summary,
        }
    }
