"""Seed helper for the GLOBAL default sandbox environment.

Importable both from the data migration and from tests. The function is idempotent
and intentionally leaves env-locked fields unset on the row — those values come
from the runtime overlay in ``sandbox_envs.services.get_global_default``.

``base_image`` is always persisted on the row (the model requires it). When set,
``DAIV_SANDBOX_BASE_IMAGE`` provides the initial seed value, but the field is not
env-locked at runtime: admins can override it via the UI. Note that re-invoking
this function (e.g. running the data migration a second time) will overwrite a
UI-edited ``base_image`` via ``update_or_create``; in practice the migration runs
once per deployment so this is theoretical.
"""

import logging

logger = logging.getLogger("daiv.sandbox_envs")

_FALLBACK_IMAGE = "python:3.12-alpine"


def seed_global_default(SandboxEnvironment) -> None:  # noqa: N803 — historical Django pattern
    from core.site_settings import site_settings

    is_locked = site_settings.is_env_locked

    persisted_kwargs: dict[str, object] = {"base_image": site_settings.sandbox_base_image or _FALLBACK_IMAGE}

    if not is_locked("sandbox_network_enabled"):
        persisted_kwargs["network_enabled"] = bool(site_settings.sandbox_network_enabled)
    if not is_locked("sandbox_cpu") and site_settings.sandbox_cpu is not None:
        persisted_kwargs["cpus"] = site_settings.sandbox_cpu
    if not is_locked("sandbox_memory") and site_settings.sandbox_memory is not None:
        persisted_kwargs["memory_bytes"] = site_settings.sandbox_memory

    SandboxEnvironment.objects.update_or_create(
        scope="global", name="Default", defaults={**persisted_kwargs, "is_default": True, "user": None}
    )
