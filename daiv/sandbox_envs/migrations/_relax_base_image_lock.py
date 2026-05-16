"""Helper for the ``0003_relax_base_image_lock`` data migration.

Extracted to a non-digit-prefixed module so it can be imported from tests and
from the migration itself. The logic replaces the placeholder ``base_image``
on the GLOBAL Default row with the ``DAIV_SANDBOX_BASE_IMAGE`` value, fixing
deployments upgraded from the era when ``base_image`` was env-locked.
"""

_PLACEHOLDER_IMAGE = "python:3.12-alpine"


def relax_base_image_lock(SandboxEnvironment) -> None:  # noqa: N803 — historical Django pattern
    from core.site_settings import site_settings

    if not site_settings.is_env_locked("sandbox_base_image"):
        return

    env_value = site_settings.sandbox_base_image
    if not env_value or env_value == _PLACEHOLDER_IMAGE:
        return

    SandboxEnvironment.objects.filter(scope="global", is_default=True, base_image=_PLACEHOLDER_IMAGE).update(
        base_image=env_value
    )
