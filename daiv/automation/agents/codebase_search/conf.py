from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.constants import ModelName


class CodebaseSearchSettings(BaseSettings):
    """
    Settings for the codebase search agent.
    """

    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="CODEBASE_SEARCH_")

    NAME: str = Field(default="CodebaseSearch", description="Name of the codebase search agent.")
    TOP_N: int = Field(default=10, description="Number of results to return from the codebase search.")
    REPHRASE_MODEL_NAME: ModelName = Field(
        default=ModelName.GPT_4O_MINI_2024_07_18, description="Model name to be used for codebase search."
    )
    REPHRASE_FALLBACK_MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_5_HAIKU_20241022, description="Fallback model name to be used for codebase search."
    )
    RERANKING_MODEL_NAME: ModelName = Field(
        default=ModelName.GPT_4O_MINI_2024_07_18, description="Model name to be used for listwise reranking."
    )
    RERANKING_FALLBACK_MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_5_HAIKU_20241022,
        description="Fallback model name to be used for listwise reranking.",
    )


settings = CodebaseSearchSettings()  # type: ignore
