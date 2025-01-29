from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ChatSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="CHAT_")

    REASONING: bool = Field(default=True, description="Whether to include reasoning in the chat completion")


settings = ChatSettings()  # type: ignore
