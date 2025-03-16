from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.constants import ModelName


class IssueAddressorSettings(BaseSettings):
    """
    Settings for the issue addressor agent.
    """

    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="ISSUE_ADDRESSOR_")

    ASSESSMENT_MODEL_NAME: ModelName = Field(
        default=ModelName.GPT_4O_MINI_2024_07_18, description="Model name to be used for issue assessment."
    )
    FALLBACK_ASSESSMENT_MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_5_HAIKU_20241022, description="Fallback model name to be used for issue assessment."
    )


settings = IssueAddressorSettings()  # type: ignore
