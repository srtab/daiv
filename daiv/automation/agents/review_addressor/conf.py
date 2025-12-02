from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.constants import ModelName


class ReviewAddressorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REVIEW_ADDRESSOR_", env_parse_none_str="None")

    NAME: str = Field(default="ReviewAddressor", description="Name of the review addressor agent shown on LangSmith.")
    REVIEW_COMMENT_MODEL_NAME: ModelName | str = Field(
        default=ModelName.GPT_4_1_MINI, description="Model to route the review comment to the correct agent."
    )
    REPLY_MODEL_NAME: ModelName | str = Field(
        default=ModelName.CLAUDE_HAIKU_4_5,
        description="Model that will interpret the review comment and reply or ask clarifying questions.",
    )
    REPLY_TEMPERATURE: float = Field(default=0.2, description="Temperature for the reply model.")
    RECURSION_LIMIT: int = Field(
        default=100, description="Recursion limit for the agent to address all the review comments in a single run."
    )


settings = ReviewAddressorSettings()  # type: ignore
