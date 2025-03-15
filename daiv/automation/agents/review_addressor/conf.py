from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.constants import ModelName


class ReviewAddressorSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="REVIEW_ADDRESSOR_")

    ASSESSMENT_MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_5_HAIKU_20241022, description="Model name to be used for review assessment."
    )
    FALLBACK_ASSESSMENT_MODEL_NAME: ModelName = Field(
        default=ModelName.GPT_4O_MINI_2024_07_18, description="Fallback model name to be used for review assessment."
    )
    REPLY_MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_5_HAIKU_20241022,
        description="Model name to be used for reply to comments or questions.",
    )
    FALLBACK_REPLY_MODEL_NAME: ModelName = Field(
        default=ModelName.GPT_4O_MINI_2024_07_18, description="Fallback model name for REPLY_MODEL_NAME."
    )
    REPLY_TEMPERATURE: float = Field(default=0.5, description="Temperature for the reply model.")


settings = ReviewAddressorSettings()  # type: ignore
