from django.core.checks import Error, register

from codebase.base import ClientType

from .conf import settings


@register("codebase")
def check_api_keys(app_configs, **kwargs):
    """
    Check if the settings are set, specially the ones that are defined as secrets.
    """
    errors = []

    if settings.CLIENT == ClientType.GITLAB and not settings.GITLAB_AUTH_TOKEN:
        errors.append(
            Error(
                f"No API key found for {settings.CLIENT}. "
                "Please set the API key using the environment variable CODEBASE_GITLAB_AUTH_TOKEN."
            )
        )

    return errors
