from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DeferredToolsSettings(BaseSettings):
    model_config = SettingsConfigDict(
        secrets_dir="/run/secrets", env_prefix="DEFERRED_TOOLS_", env_parse_none_str="None"
    )

    ENABLED: bool = Field(
        default=True,
        description=(
            "If True, tools not in the always-loaded set are deferred behind tool_search instead of bound eagerly."
        ),
    )
    TOP_K_DEFAULT: int = Field(default=3, description="Default number of search results returned by tool_search.")
    TOP_K_MAX: int = Field(default=10, description="Maximum number of search results tool_search will return per call.")


settings = DeferredToolsSettings()
