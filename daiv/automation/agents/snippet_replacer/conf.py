from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.base import CODING_COST_EFFICIENT_MODEL_NAME


class SnippetReplacerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SNIPPET_REPLACER_")

    STRATEGY: Literal["llm", "find_and_replace"] = Field(
        default="find_and_replace",
        description="Strategy to use for snippet replacement. 'llm' uses a LLM to replace the snippet."
        " 'find_and_replace' uses a find and replace strategy to replace the snippet.",
    )
    MODEL: str = Field(default=CODING_COST_EFFICIENT_MODEL_NAME, description="Model to use for snippet replacement.")


settings = SnippetReplacerSettings()
