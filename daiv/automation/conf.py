from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AutomationSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="AUTOMATION_")

    # OpenRouter settings
    OPENROUTER_API_KEY: str | None = Field(default=None, description="OpenRouter API key", alias="OPENROUTER_API_KEY")
    OPENROUTER_API_BASE: str | None = Field(
        default="https://openrouter.ai/api/v1", description="OpenRouter API base url", alias="OPENROUTER_API_BASE"
    )

    # Web search settings
    WEB_SEARCH_MAX_RESULTS: int = Field(default=5, description="Maximum number of results to return from web search")
    WEB_SEARCH_ENGINE: Literal["duckduckgo", "tavily"] = Field(
        default="duckduckgo",
        description=(
            "Web search engine to use. For Tavily, you need to set the TAVILY_API_KEY environment variable. "
            "If not set, the DuckDuckGo search engine will be used."
        ),
    )


settings = AutomationSettings()  # type: ignore
