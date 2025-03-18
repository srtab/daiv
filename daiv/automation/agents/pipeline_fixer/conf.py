from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.base import ThinkingLevel
from automation.agents.constants import ModelName


class PipelineFixerSettings(BaseSettings):
    """
    Settings for the pipeline fixer agent.
    """

    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="PIPELINE_FIXER_")

    NAME: str = Field(default="PipelineFixer", description="Name of the pipeline fixer agent.")
    MAX_ITERATIONS: int = Field(default=20, description="Maximum number of retry iterations for pipeline fixer.")
    LOG_EVALUATOR_MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_5_HAIKU_20241022, description="Model name to be used for log evaluator."
    )
    LOG_EVALUATOR_FALLBACK_MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_7_SONNET_20250219, description="Fallback model name for log evaluator."
    )
    TROUBLESHOOTING_MODEL_NAME: ModelName = Field(
        default=ModelName.O3_MINI_2025_01_31, description="Model name to be used for pipeline fixer."
    )
    TROUBLESHOOTING_THINKING_LEVEL: ThinkingLevel = Field(
        default=ThinkingLevel.HIGH, description="Thinking level to be used for pipeline fixer."
    )
    LINT_EVALUATOR_MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_5_HAIKU_20241022, description="Model name to be used for lint evaluator."
    )
    LINT_EVALUATOR_FALLBACK_MODEL_NAME: ModelName = Field(
        default=ModelName.GPT_4O_MINI_2024_07_18, description="Fallback model name for lint evaluator."
    )


settings = PipelineFixerSettings()  # type: ignore
