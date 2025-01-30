from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.constants import ModelName


class AutomationSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="AUTOMATION_")

    # Agent settings
    RECURSION_LIMIT: int = Field(default=50, description="Default recursion limit for the agent")
    PLANING_PERFORMANT_MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_5_SONNET_20241022,
        description="Model name to be used to plan tasks with high performance.",
    )
    CODING_PERFORMANT_MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_5_SONNET_20241022, description="Model name to be used to code with high performance."
    )
    CODING_COST_EFFICIENT_MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_5_HAIKU_20241022, description="Model name to be used to code with cost efficiency."
    )
    GENERIC_PERFORMANT_MODEL_NAME: ModelName = Field(
        default=ModelName.GPT_4O_2024_11_20,
        description="Model name to be used for generic tasks with high performance.",
    )
    GENERIC_COST_EFFICIENT_MODEL_NAME: ModelName = Field(
        default=ModelName.GPT_4O_MINI_2024_07_18,
        description="Model name to be used for generic tasks with cost efficiency.",
    )

    # Snippet replacer settings
    SNIPPET_REPLACER_MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_5_HAIKU_20241022, description="Model name to be used for snippet replacer."
    )
    SNIPPET_REPLACER_STRATEGY: Literal["llm", "find_and_replace"] = Field(
        default="find_and_replace",
        description="Strategy to use for snippet replacement. 'llm' uses a LLM to replace the snippet."
        " 'find_and_replace' uses a find and replace strategy to replace the snippet.",
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
    # Codebase search settings
    CODEBASE_SEARCH_TOP_N: int = Field(
        default=10, description="Maximum number of documents to return from the codebase search."
    )


settings = AutomationSettings()  # type: ignore
