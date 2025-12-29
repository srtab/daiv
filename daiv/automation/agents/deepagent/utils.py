from typing import TYPE_CHECKING, Any

from .conf import settings

if TYPE_CHECKING:
    from codebase.repo_config import DAIVModelConfig

    pass


def get_daiv_agent_kwargs(*, model_config: DAIVModelConfig, use_max: bool = False) -> dict[str, Any]:
    """
    Get DAIV agent configuration based on models configuration and use max models configuration.

    Args:
        model_config (DAIVModelConfig): The models configuration.
        use_max (bool): Whether to use the max models configuration.

    Returns:
        dict[str, Any]: Configuration kwargs for DAIVAgent.
    """
    model = model_config.model
    fallback_models = [model_config.fallback_model]
    thinking_level = model_config.thinking_level

    if use_max:
        model = settings.MAX_MODEL_NAME
        fallback_models = [model_config.model, model_config.fallback_model]
        thinking_level = settings.MAX_THINKING_LEVEL

    return {"model_names": [model] + fallback_models, "thinking_level": thinking_level}
