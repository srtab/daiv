from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AutomationSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="AUTOMATION_")

    # OpenRouter settings
    OPENROUTER_API_KEY: SecretStr | None = Field(
        default=None, description="OpenRouter API key", alias="OPENROUTER_API_KEY"
    )
    OPENROUTER_API_BASE: str | None = Field(
        default="https://openrouter.ai/api/v1", description="OpenRouter API base url", alias="OPENROUTER_API_BASE"
    )

    # Anthropic settings
    ANTHROPIC_API_KEY: SecretStr | None = Field(
        default=None, description="Anthropic API key", alias="ANTHROPIC_API_KEY"
    )

    # OpenAI settings
    OPENAI_API_KEY: SecretStr | None = Field(default=None, description="OpenAI API key", alias="OPENAI_API_KEY")

    # Google API settings
    GOOGLE_API_KEY: SecretStr | None = Field(default=None, description="Google API key", alias="GOOGLE_API_KEY")

    # Web search settings
    WEB_SEARCH_MAX_RESULTS: int = Field(default=5, description="Maximum number of results to return from web search")
    WEB_SEARCH_ENGINE: Literal["duckduckgo", "tavily"] = Field(
        default="duckduckgo",
        description=(
            "Web search engine to use. For 'tavily', you need to set the WEB_SEARCH_API_KEY environment variable."
        ),
    )
    WEB_SEARCH_API_KEY: SecretStr | None = Field(
        default=None,
        description="Web search API key. Only applies if WEB_SEARCH_ENGINE is set to 'tavily'.",
        alias="WEB_SEARCH_API_KEY",
    )


settings = AutomationSettings()  # type: ignore
