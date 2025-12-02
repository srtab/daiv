from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.constants import ModelName


class CodebaseChatSettings(BaseSettings):
    """
    Settings for the codebase chat agent.
    """

    model_config = SettingsConfigDict(env_prefix="CODEBASE_CHAT_", env_parse_none_str="None")

    NAME: str = Field(default="CodebaseChat", description="Name of the codebase chat agent.")
    MODEL_NAME: ModelName | str = Field(
        default=ModelName.GPT_5_MINI, description="Model name to be used for codebase chat."
    )
    TEMPERATURE: float = Field(default=0.2, description="Temperature to be used for codebase chat.")
    RECURSION_LIMIT: int = Field(default=50, description="Recursion limit for the codebase chat agent.")


settings = CodebaseChatSettings()  # type: ignore
