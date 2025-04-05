import os

from django.core.checks import Error, register

from automation.agents.base import BaseAgent, ModelProvider
from automation.agents.codebase_chat.conf import settings as codebase_chat_settings
from automation.agents.codebase_search.conf import settings as codebase_search_settings
from automation.agents.image_url_extractor.conf import settings as image_url_extractor_settings
from automation.agents.issue_addressor.conf import settings as issue_addressor_settings
from automation.agents.pipeline_fixer.conf import settings as pipeline_fixer_settings
from automation.agents.plan_and_execute.conf import settings as plan_and_execute_settings

declared_model_names = {
    codebase_chat_settings.MODEL_NAME,
    codebase_search_settings.REPHRASE_MODEL_NAME,
    codebase_search_settings.RERANKING_MODEL_NAME,
    image_url_extractor_settings.MODEL_NAME,
    issue_addressor_settings.ASSESSMENT_MODEL_NAME,
    pipeline_fixer_settings.LINT_EVALUATOR_MODEL_NAME,
    pipeline_fixer_settings.LOG_EVALUATOR_MODEL_NAME,
    pipeline_fixer_settings.TROUBLESHOOTING_MODEL_NAME,
    pipeline_fixer_settings.TROUBLESHOOTING_THINKING_LEVEL,
    plan_and_execute_settings.EXECUTION_MODEL_NAME,
    plan_and_execute_settings.PLANNING_MODEL_NAME,
    plan_and_execute_settings.PLAN_APPROVAL_MODEL_NAME,
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
        elif model_provider == ModelProvider.OPENROUTER and not os.environ.get("OPENROUTER_API_KEY"):
            errors.append(
                Error(
                    f"No API key found for {model_name}. "
                    "Please set the API key using the environment variable OPENROUTER_API_KEY."
                )
            )
    return errors
