from django.core.checks import Error, register

from core.models import WebSearchEngineChoices
from core.site_settings import site_settings

from .agent.base import BaseAgent, ModelProvider


@register("automation")
def check_api_keys(app_configs, **kwargs):
    """
    Check if the settings are set, specially the ones that are defined as secrets.
    """
    errors = []

    declared_model_names = {
        site_settings.agent_model_name,
        site_settings.agent_fallback_model_name,
        site_settings.diff_to_metadata_model_name,
        site_settings.diff_to_metadata_fallback_model_name,
        site_settings.web_fetch_model_name,
    }

    for model_name in declared_model_names:
        try:
            model_provider = BaseAgent.get_model_provider(model_name)
        except ValueError:
            errors.append(Error(f"Model {model_name} is not supported. Please check the model name."))
            continue

        if model_provider == ModelProvider.OPENAI and not site_settings.openai_api_key:
            errors.append(
                Error(
                    f"No API key found for {model_name}. "
                    "Please set the API key using the environment variable or docker secret OPENAI_API_KEY."
                )
            )
        elif model_provider == ModelProvider.GOOGLE_GENAI and not site_settings.google_api_key:
            errors.append(
                Error(
                    f"No API key found for {model_name}. "
                    "Please set the API key using the environment variable or docker secret GOOGLE_API_KEY."
                )
            )
        elif model_provider == ModelProvider.ANTHROPIC and not site_settings.anthropic_api_key:
            errors.append(
                Error(
                    f"No API key found for {model_name}. "
                    "Please set the API key using the environment variable or docker secret ANTHROPIC_API_KEY."
                )
            )
        elif model_provider == ModelProvider.OPENROUTER and not site_settings.openrouter_api_key:
            errors.append(
                Error(
                    f"No API key found for {model_name}. "
                    "Please set the API key using the environment variable or docker secret OPENROUTER_API_KEY."
                )
            )

    if site_settings.web_search_engine == WebSearchEngineChoices.TAVILY and not site_settings.web_search_api_key:
        errors.append(
            Error(
                f"No API key found for {site_settings.web_search_engine}. "
                "Please set the API key using the environment variable or docker secret WEB_SEARCH_API_KEY."
            )
        )

    return errors
