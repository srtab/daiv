"""Seed helper for the GLOBAL default sandbox environment.

Importable both from the data migration and from tests. The function is idempotent
and intentionally leaves env-locked fields unset on the row — those values come
from the runtime overlay in ``sandbox_envs.services.get_global_default``.
"""

import logging

logger = logging.getLogger("daiv.sandbox_envs")

_FALLBACK_IMAGE = "python:3.12-alpine"


def seed_global_default(SandboxEnvironment) -> None:  # noqa: N803 — historical Django pattern
    from core.site_settings import site_settings

    is_locked = site_settings.is_env_locked

    persisted_kwargs: dict[str, object] = {}
    if not is_locked("sandbox_base_image"):
        image = site_settings.sandbox_base_image or _FALLBACK_IMAGE
        persisted_kwargs["base_image"] = image
    else:
        # Model requires non-empty ``base_image``, so we persist a placeholder.
        # The runtime overlay in ``get_global_default`` still wins at read time
        # because the field is env-locked.
        persisted_kwargs["base_image"] = _FALLBACK_IMAGE
        logger.info(
            "sandbox_envs seed: base_image is env-locked; row holds placeholder, "
            "DAIV_SANDBOX_BASE_IMAGE remains authoritative at runtime."
        )

    if not is_locked("sandbox_network_enabled"):
        persisted_kwargs["network_enabled"] = bool(site_settings.sandbox_network_enabled)
    if not is_locked("sandbox_cpu") and site_settings.sandbox_cpu is not None:
        persisted_kwargs["cpus"] = site_settings.sandbox_cpu
    if not is_locked("sandbox_memory") and site_settings.sandbox_memory is not None:
        persisted_kwargs["memory_bytes"] = site_settings.sandbox_memory

    SandboxEnvironment.objects.update_or_create(
        scope="global", name="Default", defaults={**persisted_kwargs, "is_default": True, "user": None}
    )
