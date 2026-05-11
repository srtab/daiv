from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sandbox_envs.models import SandboxEnvironment, Scope

if TYPE_CHECKING:
    from codebase.context import SandboxRuntime
    from codebase.repo_config import Sandbox

logger = logging.getLogger("daiv.sandbox_envs")


_FIELD_TO_SETTINGS: dict[str, str] = {
    "base_image": "sandbox_base_image",
    "network_enabled": "sandbox_network_enabled",
    "memory_bytes": "sandbox_memory",
    "cpus": "sandbox_cpu",
}


def get_locked_runtime_fields() -> frozenset[str]:
    """Return the set of SandboxRuntime field names locked by DAIV_SANDBOX_* env vars.

    Locked fields cannot be overridden by per-run envs or ``.daiv.yml``; only the
    GLOBAL default (already overlaid with the env-var values in
    :func:`get_global_default`) is consulted.
    """
    from core.site_settings import site_settings

    return frozenset(field for field, setting in _FIELD_TO_SETTINGS.items() if site_settings.is_env_locked(setting))


@dataclass(frozen=True)
class SandboxEnvOverride:
    """A resolved sandbox-env view with secrets decrypted. Internal — only the
    merge resolver in ``merge_sandbox_runtime`` should construct or read these."""

    base_image: str | None
    network_enabled: bool | None
    memory_bytes: int | None
    cpus: float | None
    env_vars: dict[str, str]


def _row_to_override(env: SandboxEnvironment) -> SandboxEnvOverride:
    from core.encryption import DecryptionError

    try:
        env_vars_rows = env.env_vars or []
    except DecryptionError:
        # Agent run paths must not crash on a key rotation; drop env vars and
        # keep going. The descriptor already logs at exception level.
        logger.error("env_vars decryption failed for SandboxEnvironment id=%s; dropping env_vars", env.id)
        env_vars_rows = []
    return SandboxEnvOverride(
        base_image=env.base_image or None,
        network_enabled=env.network_enabled,
        memory_bytes=env.memory_bytes,
        cpus=float(env.cpus) if isinstance(env.cpus, Decimal) else env.cpus,
        env_vars={
            entry["name"]: entry["value"]
            for entry in env_vars_rows
            if entry.get("name") and entry.get("value") is not None
        },
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
    return _row_to_override(env)


async def get_global_default() -> SandboxEnvOverride | None:
    """Resolved GLOBAL default — row values for unlocked fields, env-var overlay
    for env-locked fields. Returns ``None`` only when nothing is configurable
    (no row AND no env-locked overlay)."""
    from core.site_settings import site_settings

    row = await SandboxEnvironment.objects.filter(scope=Scope.GLOBAL, is_default=True).afirst()
    locked = site_settings.is_env_locked

    locks_present = any(
        locked(n) for n in ("sandbox_base_image", "sandbox_network_enabled", "sandbox_cpu", "sandbox_memory")
    )
    if row is None and not locks_present:
        return None

    base = (
        _row_to_override(row)
        if row is not None
        else SandboxEnvOverride(base_image=None, network_enabled=None, memory_bytes=None, cpus=None, env_vars={})
    )

    return SandboxEnvOverride(
        base_image=(site_settings.sandbox_base_image if locked("sandbox_base_image") else base.base_image),
        network_enabled=(
            bool(site_settings.sandbox_network_enabled) if locked("sandbox_network_enabled") else base.network_enabled
        ),
        memory_bytes=(site_settings.sandbox_memory if locked("sandbox_memory") else base.memory_bytes),
        cpus=(site_settings.sandbox_cpu if locked("sandbox_cpu") else base.cpus),
        env_vars=base.env_vars,
    )


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

    from django.db.models import Q

    qs = SandboxEnvironment.objects.filter(Q(scope=Scope.USER, user=user) | Q(scope=Scope.GLOBAL))
    if looks_like_uuid(name_or_id):
        env = await qs.filter(pk=name_or_id).afirst()
        if env is not None:
            return env
    env = await qs.filter(name=name_or_id).afirst()
    if env is None:
        valid = [n async for n in qs.values_list("name", flat=True)]
        raise LookupError(f"unknown environment '{name_or_id}'; valid: {valid}")
    return env


def merge_sandbox_runtime(
    repo_sandbox: Sandbox,
    repo_fields_set: frozenset[str],
    per_run: SandboxEnvOverride | None,
    global_default: SandboxEnvOverride | None,
    locked_fields: frozenset[str] = frozenset(),
) -> SandboxRuntime:
    """Per-field precedence: per_run > .daiv.yml (explicit key) > global_default.

    A field is taken from per_run only when its value is non-None. A field is
    taken from .daiv.yml only when the key was literally present in the YAML
    (so ``base_image: null`` is "explicitly disabled" and beats global).

    Fields in ``locked_fields`` (DAIV_SANDBOX_* env-locked) skip both per_run
    and ``.daiv.yml``; only ``global_default`` (already overlaid with the
    env-var values by :func:`get_global_default`) is consulted.
    """
    from codebase.context import SandboxRuntime

    def pick(field: str, runtime_default):
        if field not in locked_fields:
            if per_run is not None:
                v = getattr(per_run, field)
                if v is not None:
                    return v
            if field in repo_fields_set:
                return getattr(repo_sandbox, field)
        if global_default is not None:
            v = getattr(global_default, field)
            if v is not None:
                return v
        return runtime_default

    return SandboxRuntime(
        base_image=pick("base_image", None),
        network_enabled=pick("network_enabled", False),
        memory_bytes=pick("memory_bytes", None),
        cpus=pick("cpus", None),
        env_vars={**(global_default.env_vars if global_default else {}), **(per_run.env_vars if per_run else {})},
        command_policy=repo_sandbox.command_policy,
    )
