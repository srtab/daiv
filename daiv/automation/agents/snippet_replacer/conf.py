from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.constants import ModelName


class SnippetReplacerSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="SNIPPET_REPLACER_")

    MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_5_HAIKU_20241022, description="Model name to be used for snippet replacer."
    )
    STRATEGY: Literal["llm", "find_and_replace"] = Field(
        default="find_and_replace",
        description="Strategy to use for snippet replacement. 'llm' uses a LLM to replace the snippet."
        " 'find_and_replace' uses a find and replace strategy to replace the snippet.",
    )


settings = SnippetReplacerSettings()  # type: ignore
