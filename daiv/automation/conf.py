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

    # Tools settings
    web_search_max_results: int = Field(default=5, description="Maximum number of results to return from web search")
    codebase_search_max_transformations: int = Field(
        default=2, description="Maximum number of transformations to apply to the query."
    )


settings = AutomationSettings()  # type: ignore
