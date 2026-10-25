from __future__ import annotations

from django import template

register = template.Library()


@register.simple_tag
def social_consent_platform() -> str:
    """The platform whose wider grant the sign-in page discloses while cross-project access is on, else ``""``."""
    from accounts.credentials import OAUTH_CAPABLE_PLATFORMS
    from codebase.conf import settings as codebase_settings
    from core.site_settings import site_settings

    platform = codebase_settings.CLIENT
    if platform not in OAUTH_CAPABLE_PLATFORMS or not site_settings.cross_project_access_enabled:
        return ""
    return platform.value.capitalize()
