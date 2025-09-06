from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.constants import ModelName


class ReviewAddressorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REVIEW_ADDRESSOR_")

    NAME: str = Field(default="ReviewAddressor", description="Name of the review addressor agent.")
    REVIEW_COMMENT_MODEL_NAME: ModelName | str = Field(
        default=ModelName.GPT_4_1_MINI, description="Model name to be used for review assessment."
    )
    REPLY_MODEL_NAME: ModelName | str = Field(
        default=ModelName.GPT_4_1, description="Model name to be used for reply to comments or questions."
    )
    REPLY_TEMPERATURE: float = Field(default=0.2, description="Temperature for the reply model.")


settings = ReviewAddressorSettings()  # type: ignore
