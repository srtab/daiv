from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.constants import ModelName


class SnippetReplacerSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="SNIPPET_REPLACER_")

    NAME: str = Field(default="SnippetReplacer", description="Name of the snippet replacer agent.")
    MODEL_NAME: ModelName | str = Field(
        default=ModelName.CLAUDE_3_5_HAIKU,
        description="Model name to be used for snippet replacer. This model is used for the LLM strategy.",
    )
    STRATEGY: Literal["llm", "find_and_replace"] = Field(
        default="find_and_replace",
        description="Strategy to use for snippet replacement. 'llm' uses a LLM to replace the snippet."
        " 'find_and_replace' uses a find and replace strategy to replace the snippet.",
    )


settings = SnippetReplacerSettings()  # type: ignore
