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
    FROZEN_TOOLS_MODELS: list[str] = Field(
        default=["claude-", "anthropic/claude-", "qwen/qwen3.8-max"],
        description=(
            "Prefix-matched model names that get a frozen tools array; schemas reach them only via "
            "tool_search results. Empty list disables freezing."
        ),
    )
    INLINE_TOOLS_MODELS: list[str] = Field(
        default=[
            "claude-opus-5",
            "claude-fable-5",
            "claude-mythos-5",
            "claude-opus-4-8",
            "claude-sonnet-5-5",
            "gpt-6-",
            "gpt-5.6-",
        ],
        description=(
            "Prefix-matched model names that get each loaded tool declared mid-conversation, right after the "
            "tool_search result that loaded it, through the provider's native mechanism (Anthropic tool_addition, "
            "OpenAI Responses additional_tools); the tools array stays frozen. Applies only to models reached "
            "through the Anthropic API or the OpenAI Responses API directly. Empty list disables it."
        ),
    )
    EMBED_SCHEMAS_IN_RESULTS: bool = Field(
        default=True,
        description=(
            "Emergency valve: if False, tool_search results carry summaries only and freezing and inline "
            "definitions are forced off, so every model falls back to the array-append + summary pre-change "
            "behaviour."
        ),
    )


settings = DeferredToolsSettings()
