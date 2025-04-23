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
    SAME_ERROR_MODEL_NAME: ModelName | str = Field(
        default=ModelName.GPT_4_1_MINI, description="Model name to be used for same error evaluator."
    )
    TROUBLESHOOTING_MODEL_NAME: ModelName | str = Field(
        default=ModelName.O4_MINI, description="Model name to be used for pipeline fixer."
    )
    TROUBLESHOOTING_THINKING_LEVEL: ThinkingLevel = Field(
        default=ThinkingLevel.HIGH, description="Thinking level to be used for pipeline fixer."
    )
    COMMAND_OUTPUT_MODEL_NAME: ModelName | str = Field(
        default=ModelName.GPT_4_1_MINI, description="Model name to be used for command output evaluator."
    )


settings = PipelineFixerSettings()  # type: ignore
