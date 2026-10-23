from django.core.checks import Error, register

from codebase.base import GitPlatform
from codebase.conf import settings as codebase_settings

from .conf import settings


@register("webhooks")
def check_webhook_secret(app_configs, **kwargs):
    """
    Check that the active platform's webhook secret is set: without it, its webhook endpoint rejects every request.
    """
    if codebase_settings.CLIENT == GitPlatform.GITLAB and not settings.GITLAB_WEBHOOK_SECRET:
        return [_missing_secret("CODEBASE_GITLAB_WEBHOOK_SECRET", "webhooks.E001")]

    if codebase_settings.CLIENT == GitPlatform.GITHUB and not settings.GITHUB_WEBHOOK_SECRET:
        return [_missing_secret("CODEBASE_GITHUB_WEBHOOK_SECRET", "webhooks.E002")]

    return []


def _missing_secret(env_var: str, check_id: str) -> Error:
    return Error(
        f"No webhook secret found for {codebase_settings.CLIENT}, so every webhook request will be rejected.",
        hint=f"Set the environment variable {env_var} to the secret configured on the webhook.",
        id=check_id,
    )
