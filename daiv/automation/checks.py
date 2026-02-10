from django.core.checks import Error, register

from .agent.base import BaseAgent, ModelProvider
from .agent.conf import settings as agent_settings
from .agent.diff_to_metadata.conf import settings as diff_to_metadata_settings
from .conf import settings

declared_model_names = {
    agent_settings.MODEL_NAME,
    agent_settings.FALLBACK_MODEL_NAME,
    diff_to_metadata_settings.MODEL_NAME,
    diff_to_metadata_settings.FALLBACK_MODEL_NAME,
    settings.WEB_FETCH_MODEL_NAME,
}


@register("automation")
def check_api_keys(app_configs, **kwargs):
    """
    Check if the settings are set, specially the ones that are defined as secrets.
    """
    errors = []

    for model_name in declared_model_names:
        try:
            model_provider = BaseAgent.get_model_provider(model_name)
        except ValueError:
            errors.append(Error(f"Model {model_name} is not supported. Please check the model name."))
            continue

        if model_provider == ModelProvider.OPENAI and not settings.OPENAI_API_KEY:
            errors.append(
                Error(
                    f"No API key found for {model_name}. "
                    "Please set the API key using the environment variable or docker secret OPENAI_API_KEY."
                )
            )
        elif model_provider == ModelProvider.GOOGLE_GENAI and not settings.GOOGLE_API_KEY:
            errors.append(
                Error(
                    f"No API key found for {model_name}. "
                    "Please set the API key using the environment variable or docker secret GOOGLE_API_KEY."
                )
            )
        elif model_provider == ModelProvider.ANTHROPIC and not settings.ANTHROPIC_API_KEY:
            errors.append(
                Error(
                    f"No API key found for {model_name}. "
                    "Please set the API key using the environment variable or docker secret ANTHROPIC_API_KEY."
                )
            )
        elif model_provider == ModelProvider.OPENROUTER and not settings.OPENROUTER_API_KEY:
            errors.append(
                Error(
                    f"No API key found for {model_name}. "
                    "Please set the API key using the environment variable or docker secret OPENROUTER_API_KEY."
                )
            )

    if settings.WEB_SEARCH_ENGINE == "tavily" and not settings.WEB_SEARCH_API_KEY:
        errors.append(
            Error(
                f"No API key found for {settings.WEB_SEARCH_ENGINE}. "
                "Please set the API key using the environment variable or docker secret WEB_SEARCH_API_KEY."
            )
        )

    return errors
