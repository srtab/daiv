import os

from django.core.checks import Error, register

from automation.agents.base import BaseAgent, ModelProvider

from .conf import settings

declared_model_names = {
    settings.CODING_COST_EFFICIENT_MODEL_NAME,
    settings.CODING_PERFORMANT_MODEL_NAME,
    settings.GENERIC_COST_EFFICIENT_MODEL_NAME,
    settings.GENERIC_PERFORMANT_MODEL_NAME,
    settings.PLANING_PERFORMANT_MODEL_NAME,
    settings.SNIPPET_REPLACER_MODEL_NAME,
}


@register("automation")
def check_api_keys(app_configs, **kwargs):
    """
    Check if the API keys for the models are set.
    """
    errors = []
    for model_name in declared_model_names:
        model_provider = BaseAgent.get_model_provider(model_name)
        if model_provider == ModelProvider.OPENAI and not os.environ.get("OPENAI_API_KEY"):
            errors.append(
                Error(
                    f"No API key found for {model_name}. "
                    "Please set the API key using the environment variable OPENAI_API_KEY."
                )
            )
        elif model_provider == ModelProvider.DEEPSEEK and not settings.DEEPSEEK_API_KEY:
            errors.append(
                Error(
                    f"No API key found for {model_name}. "
                    "Please set the API key using the environment variable DEEPSEEK_API_KEY."
                )
            )
        elif model_provider == ModelProvider.GOOGLE_GENAI and not os.environ.get("GOOGLE_API_KEY"):
            errors.append(
                Error(
                    f"No API key found for {model_name}. "
                    "Please set the API key using the environment variable GOOGLE_API_KEY."
                )
            )
        elif model_provider == ModelProvider.ANTHROPIC and not os.environ.get("ANTHROPIC_API_KEY"):
            errors.append(
                Error(
                    f"No API key found for {model_name}. "
                    "Please set the API key using the environment variable ANTHROPIC_API_KEY."
                )
            )
    return errors
