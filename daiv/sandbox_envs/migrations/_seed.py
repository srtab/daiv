"""Seed helper for the GLOBAL default sandbox environment.

Idempotent: re-running the migration is a no-op for existing rows.
"""

import logging

logger = logging.getLogger("daiv.sandbox_envs")

_FALLBACK_IMAGE = "ghcr.io/astral-sh/uv:python3.14-bookworm"


def seed_global_default(SandboxEnvironment) -> None:  # noqa: N803
    SandboxEnvironment.objects.get_or_create(
        scope="global", name="Default", defaults={"base_image": _FALLBACK_IMAGE, "is_default": True, "user": None}
    )
