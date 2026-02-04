from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agent.constants import ModelName


class AutomationSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="AUTOMATION_", env_parse_none_str="None")

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
        default=None, description="Web search API key. Only applies if WEB_SEARCH_ENGINE is set to 'tavily'."
    )

    # Web fetch settings
    WEB_FETCH_ENABLED: bool = Field(default=True, description="Enable/disable the native web_fetch tool.")
    WEB_FETCH_MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_HAIKU_4_5,
        description=(
            "Model name used by web_fetch to process page content with the prompt. "
            "Set to None to return the raw page content instead of processing it."
        ),
    )
    WEB_FETCH_CACHE_TTL_SECONDS: int = Field(
        default=15 * 60, description="TTL for web_fetch cache entries, in seconds."
    )
    WEB_FETCH_TIMEOUT_SECONDS: int = Field(default=15, description="HTTP timeout for web_fetch, in seconds.")
    WEB_FETCH_PROXY_URL: str | None = Field(default=None, description="Optional proxy URL for web_fetch HTTP requests.")
    WEB_FETCH_MAX_CONTENT_CHARS: int = Field(
        default=50_000,
        description=(
            "Maximum page content size (in characters) to analyze in one pass. Larger pages return a guidance message."
        ),
    )


settings = AutomationSettings()
