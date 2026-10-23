from __future__ import annotations

from typing import Any


def social_consent(request) -> dict[str, Any]:
    """Name the platform whose wider grant the sign-in page discloses while cross-project access is on."""
    from codebase.base import GitPlatform
    from codebase.conf import settings as codebase_settings
    from core.site_settings import site_settings

    platform = codebase_settings.CLIENT
    if platform not in (GitPlatform.GITLAB, GitPlatform.GITHUB) or not site_settings.cross_project_access_enabled:
        return {}
    return {"socialaccount_platform": platform.value.capitalize()}
