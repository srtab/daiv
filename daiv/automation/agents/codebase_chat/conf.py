from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.constants import ModelName


class CodebaseChatSettings(BaseSettings):
    """
    Settings for the codebase chat agent.
    """

    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="CODEBASE_CHAT_")

    NAME: str = Field(default="CodebaseChat", description="Name of the codebase chat agent.")
    MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_5_HAIKU_20241022, description="Model name to be used for codebase chat."
    )
    TEMPERATURE: float = Field(default=0.5, description="Temperature to be used for codebase chat.")


settings = CodebaseChatSettings()  # type: ignore
