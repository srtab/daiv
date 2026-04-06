from django.core.checks import Error, register

from core.models import SiteConfiguration, WebSearchEngineChoices
from core.site_settings import site_settings

from .agent.base import BaseAgent, ModelProvider


@register("automation")
def check_api_keys(app_configs, **kwargs):
    """
    Check if the settings are set, specially the ones that are defined as secrets.
    """
    errors = []

    for field_name in SiteConfiguration.MODEL_NAME_FIELDS:
        model_name = getattr(site_settings, field_name)
        if not model_name:
            continue
        try:
            provider = BaseAgent.get_model_provider(model_name)
        except ValueError:
            errors.append(Error(f"Model {model_name} is not supported. Please check the model name."))
            continue
        key_field = ModelProvider.api_key_field_for(provider)
        if key_field and not getattr(site_settings, key_field, None):
            env_var = site_settings.get_env_var_name(key_field).upper()
            errors.append(
                Error(
                    f"No API key found for {model_name}. "
                    f"Please set the API key using the environment variable or docker secret {env_var}."
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
