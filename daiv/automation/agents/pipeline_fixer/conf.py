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
    LOG_EVALUATOR_MODEL_NAME: ModelName | str = Field(
        default=ModelName.CLAUDE_3_5_HAIKU, description="Model name to be used for log evaluator."
    )
    TROUBLESHOOTING_MODEL_NAME: ModelName | str = Field(
        default=ModelName.O3_MINI, description="Model name to be used for pipeline fixer."
    )
    TROUBLESHOOTING_THINKING_LEVEL: ThinkingLevel = Field(
        default=ThinkingLevel.HIGH, description="Thinking level to be used for pipeline fixer."
    )
    LINT_EVALUATOR_MODEL_NAME: ModelName | str = Field(
        default=ModelName.CLAUDE_3_5_HAIKU, description="Model name to be used for lint evaluator."
    )


settings = PipelineFixerSettings()  # type: ignore
