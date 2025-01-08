from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AutomationSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="AUTOMATION_")

    # Agent settings
    recursion_limit: int = Field(default=50, description="Default recursion limit for the agent")
    planing_performant_model_name: str = Field(
        default="claude-3-5-sonnet-20241022", description="Model name to be used to plan tasks with high performance."
    )
    coding_performant_model_name: str = Field(
        default="claude-3-5-sonnet-20241022", description="Model name to be used to code with high performance."
    )
    coding_cost_efficient_model_name: str = Field(
        default="claude-3-5-haiku-20241022", description="Model name to be used to code with cost efficiency."
    )
    generic_performant_model_name: str = Field(
        default="gpt-4o-2024-11-20", description="Model name to be used for generic tasks with high performance."
    )
    generic_cost_efficient_model_name: str = Field(
        default="gpt-4o-mini-2024-07-18", description="Model name to be used for generic tasks with cost efficiency."
    )

    # Snippet replacer settings
    snippet_replacer_model_name: str = Field(
        default=coding_cost_efficient_model_name, description="Model name to be used for snippet replacer."
    )
    snippet_replacer_strategy: Literal["llm", "find_and_replace"] = Field(
        default="find_and_replace",
        description="Strategy to use for snippet replacement. 'llm' uses a LLM to replace the snippet."
        " 'find_and_replace' uses a find and replace strategy to replace the snippet.",
    )

    # Web search settings
    web_search_max_results: int = Field(default=5, description="Maximum number of results to return from web search")

    # Codebase search settings
    codebase_search_max_transformations: int = Field(
        default=2, description="Maximum number of transformations to apply to the query."
    )


settings = AutomationSettings()  # type: ignore
