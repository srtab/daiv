from django.core.checks import Error, register

from codebase.base import GitPlatform

from .conf import settings


@register("codebase")
def check_api_keys(app_configs, **kwargs):
    """
    Check if the settings are set, specially the ones that are defined as secrets.
    """
    errors = []

    if settings.CLIENT == GitPlatform.GITLAB and not settings.GITLAB_AUTH_TOKEN:
        errors.append(
            Error(
                f"No API key found for {settings.CLIENT}. "
                "Please set the API key using the environment variable CODEBASE_GITLAB_AUTH_TOKEN."
            )
        )

    elif settings.CLIENT == GitPlatform.GITHUB and (
        not settings.GITHUB_PRIVATE_KEY or not settings.GITHUB_APP_ID or not settings.GITHUB_INSTALLATION_ID
    ):
        errors.append(
            Error(
                f"No API key found for {settings.CLIENT}. "
                "Please set the API key using the environment variable "
                "CODEBASE_GITHUB_PRIVATE_KEY, CODEBASE_GITHUB_APP_ID, and CODEBASE_GITHUB_INSTALLATION_ID."
            )
        )

    # The webhook secret gates the unauthenticated callback endpoint: when it is
    # unset the validators fail closed, so the active platform's webhooks are dead
    # weight. Surface that as a deployment error rather than silently dropping hooks.
    if settings.CLIENT == GitPlatform.GITLAB and not settings.GITLAB_WEBHOOK_SECRET:
        errors.append(
            Error(
                "No webhook secret configured for GitLab. "
                "Webhook validation fails closed without it, so callbacks will be rejected. "
                "Set it using the environment variable CODEBASE_GITLAB_WEBHOOK_SECRET."
            )
        )
    elif settings.CLIENT == GitPlatform.GITHUB and not settings.GITHUB_WEBHOOK_SECRET:
        errors.append(
            Error(
                "No webhook secret configured for GitHub. "
                "Webhook validation fails closed without it, so callbacks will be rejected. "
                "Set it using the environment variable CODEBASE_GITHUB_WEBHOOK_SECRET."
            )
        )

    # SWE client doesn't require any API keys

    return errors
