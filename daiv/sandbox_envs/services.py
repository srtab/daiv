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


@dataclass(frozen=True)
class SandboxEnvOverride:
    """A resolved sandbox-env view with secrets decrypted. Internal — only the
    merge resolver in ``set_runtime_ctx`` should construct or read these."""

    base_image: str | None
    network_enabled: bool | None
    memory_bytes: int | None
    cpus: float | None
    env_vars: dict[str, str]


def _row_to_override(env: SandboxEnvironment) -> SandboxEnvOverride:
    return SandboxEnvOverride(
        base_image=env.base_image or None,
        network_enabled=env.network_enabled,
        memory_bytes=env.memory_bytes,
        cpus=float(env.cpus) if isinstance(env.cpus, Decimal) else env.cpus,
        env_vars={
            entry["name"]: entry["value"]
            for entry in (env.env_vars or [])
            if entry.get("name") and entry.get("value") is not None
        },
    )


async def resolve_sandbox_env(env_id: str | None) -> SandboxEnvOverride | None:
    """Load the explicit per-run env, or ``None`` if absent / deleted."""
    if not env_id:
        return None
    try:
        UUID(env_id)
    except TypeError, ValueError:
        return None
    env = await SandboxEnvironment.objects.filter(pk=env_id).afirst()
    if env is None:
        logger.warning("Per-run sandbox env %s not found; falling back to defaults", env_id)
        return None
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


def merge_sandbox_runtime(
    repo_sandbox: Sandbox,
    repo_fields_set: frozenset[str],
    per_run: SandboxEnvOverride | None,
    global_default: SandboxEnvOverride | None,
) -> SandboxRuntime:
    """Per-field precedence: per_run > .daiv.yml (explicit key) > global_default.

    A field is taken from per_run only when its value is non-None. A field is
    taken from .daiv.yml only when the key was literally present in the YAML
    (so ``base_image: null`` is "explicitly disabled" and beats global).
    """
    from codebase.context import SandboxRuntime

    def pick(field: str, runtime_default):
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
