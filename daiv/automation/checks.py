from django.core.checks import Error, register

from .agents.base import BaseAgent, ModelProvider
from .agents.codebase_chat.conf import settings as codebase_chat_settings
from .agents.pipeline_fixer.conf import settings as pipeline_fixer_settings
from .agents.plan_and_execute.conf import settings as plan_and_execute_settings
from .agents.pr_describer.conf import settings as pr_describer_settings
from .agents.review_addressor.conf import settings as review_addressor_settings
from .conf import settings

declared_model_names = {
    codebase_chat_settings.MODEL_NAME,
    pipeline_fixer_settings.COMMAND_OUTPUT_MODEL_NAME,
    pipeline_fixer_settings.TROUBLESHOOTING_MODEL_NAME,
    plan_and_execute_settings.EXECUTION_MODEL_NAME,
    plan_and_execute_settings.PLANNING_MODEL_NAME,
    plan_and_execute_settings.HUMAN_APPROVAL_MODEL_NAME,
    pr_describer_settings.MODEL_NAME,
    review_addressor_settings.REPLY_MODEL_NAME,
    review_addressor_settings.REVIEW_COMMENT_MODEL_NAME,
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
