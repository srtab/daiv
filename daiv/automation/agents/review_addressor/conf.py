from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.constants import ModelName


class ReviewAddressorSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="REVIEW_ADDRESSOR_")

    NAME: str = Field(default="ReviewAddressor", description="Name of the review addressor agent.")
    ASSESSMENT_MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_5_HAIKU, description="Model name to be used for review assessment."
    )
    REPLY_MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_5_HAIKU, description="Model name to be used for reply to comments or questions."
    )
    REPLY_TEMPERATURE: float = Field(default=0.5, description="Temperature for the reply model.")


settings = ReviewAddressorSettings()  # type: ignore
