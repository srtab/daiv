from typing import TYPE_CHECKING

from .conf import settings as plan_and_execute_settings

if TYPE_CHECKING:
    from codebase.repo_config import Models


def get_plan_and_execute_agent_kwargs(*, models_config: Models, use_max: bool = False) -> dict:
    """
    Get agent configuration based on models configuration and use max models configuration.

    Args:
        models_config (Models): The models configuration.
        use_max (bool): Whether to use the max models configuration.

    Returns:
        dict: Configuration kwargs for PlanAndExecuteAgent.
    """
    kwargs: dict = {}
    model_config = models_config.plan_and_execute

    planning_model = model_config.planning_model
    planning_fallback_model = model_config.planning_fallback_model
    execution_model = model_config.execution_model
    execution_fallback_model = model_config.execution_fallback_model
    planning_thinking_level = model_config.planning_thinking_level
    execution_thinking_level = model_config.execution_thinking_level

    if use_max:
        planning_model = plan_and_execute_settings.MAX_PLANNING_MODEL_NAME
        planning_fallback_model = model_config.planning_model
        execution_model = plan_and_execute_settings.MAX_EXECUTION_MODEL_NAME
        execution_fallback_model = model_config.execution_model
        planning_thinking_level = plan_and_execute_settings.MAX_PLANNING_THINKING_LEVEL
        execution_thinking_level = plan_and_execute_settings.MAX_EXECUTION_THINKING_LEVEL

    kwargs["planning_model_names"] = [planning_model, planning_fallback_model]
    kwargs["execution_model_names"] = [execution_model, execution_fallback_model]
    kwargs["planning_thinking_level"] = planning_thinking_level
    kwargs["execution_thinking_level"] = execution_thinking_level

    return kwargs
