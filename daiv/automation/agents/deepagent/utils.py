from pathlib import Path
from typing import TYPE_CHECKING, Any

from .conf import settings

if TYPE_CHECKING:
    from deepagents.backends.protocol import BACKEND_TYPES
    from deepagents.middleware.filesystem import FileData

    from codebase.repo_config import DAIVModelConfig

from automation.agents.constants import BUILTIN_SKILLS_DIR, BUILTIN_SKILLS_PATH


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


def copy_builtin_skills_to_backend(backend: BACKEND_TYPES) -> dict[str, FileData]:
    """
    Copy builtin skills to the /skills/ directory.

    Args:
        backend: The backend to use for copying the builtin skills.

    Returns:
        A dictionary of the files that were copied to the backend.
    """
    files_to_update = {}
    for builtin_skill_dir in BUILTIN_SKILLS_DIR.iterdir():
        for root, _dirs, files in builtin_skill_dir.walk():
            for file in files:
                source_path = Path(root) / Path(file)
                dest_path = Path(BUILTIN_SKILLS_PATH) / source_path.relative_to(BUILTIN_SKILLS_DIR)
                write_result = backend.write(str(dest_path), source_path.read_text())
                if write_result.files_update is not None:
                    files_to_update.update(write_result.files_update)
    return files_to_update
