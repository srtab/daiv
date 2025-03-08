from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.base import ThinkingLevel
from automation.agents.constants import ModelName


class PRDescriberSettings(BaseSettings):
    MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_5_HAIKU_20241022, description="Model name to be used for PR describer."
    )
    FALLBACK_MODEL_NAME: ModelName = Field(
        default=ModelName.GPT_4O_MINI_2024_07_18, description="Fallback model name to be used for PR describer."
    )


class IssueAddressorSettings(BaseSettings):
    ASSESSMENT_MODEL_NAME: ModelName = Field(
        default=ModelName.GPT_4O_MINI_2024_07_18, description="Model name to be used for issue assessment."
    )
    FALLBACK_ASSESSMENT_MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_5_HAIKU_20241022, description="Fallback model name to be used for issue assessment."
    )


class ReviewAddressorSettings(BaseSettings):
    ASSESSMENT_MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_5_HAIKU_20241022, description="Model name to be used for review assessment."
    )
    FALLBACK_ASSESSMENT_MODEL_NAME: ModelName = Field(
        default=ModelName.GPT_4O_MINI_2024_07_18, description="Fallback model name to be used for review assessment."
    )
    REPLY_MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_5_HAIKU_20241022,
        description="Model name to be used for reply to comments or questions.",
    )
    FALLBACK_REPLY_MODEL_NAME: ModelName = Field(
        default=ModelName.GPT_4O_MINI_2024_07_18, description="Fallback model name for REPLY_MODEL_NAME."
    )


class PipelineFixerSettings(BaseSettings):
    """
    Settings for the pipeline fixer agent.
    """

    MODEL_NAME: ModelName = Field(
        default=ModelName.O3_MINI_2025_01_31, description="Model name to be used for pipeline fixer."
    )
    THINKING_LEVEL: ThinkingLevel = Field(
        default=ThinkingLevel.HIGH, description="Thinking level to be used for pipeline fixer."
    )


class PlanAndExecuteSettings(BaseSettings):
    """
    Settings for the plan and execute agent.
    """

    PLANNING_MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_7_SONNET_20250219, description="Model name to be used to plan tasks."
    )
    PLANNING_THINKING_LEVEL: ThinkingLevel = Field(
        default=ThinkingLevel.MEDIUM, description="Thinking level to be used to plan tasks."
    )
    EXECUTION_MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_7_SONNET_20250219, description="Model name to be used to execute tasks."
    )
    EXECUTION_THINKING_LEVEL: ThinkingLevel = Field(
        default=ThinkingLevel.MEDIUM, description="Thinking level to be used to execute tasks."
    )
    PLAN_APPROVAL_MODEL_NAME: ModelName = Field(
        default=ModelName.GPT_4O_MINI_2024_07_18, description="Model name to be used to evaluate the plan approval."
    )


class AutomationSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="AUTOMATION_")

    # Agent settings
    RECURSION_LIMIT: int = Field(default=50, description="Default recursion limit for the agent")
    PLANING_PERFORMANT_MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_7_SONNET_20250219,
        description="Model name to be used to plan tasks with high performance.",
    )
    CODING_PERFORMANT_MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_7_SONNET_20250219, description="Model name to be used to code with high performance."
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

    # Pipeline fixer settings
    PIPELINE_FIXER_MAX_RETRY: int = Field(
        default=20, description="Maximum number of retry iterations for pipeline fixer"
    )

    PR_DESCRIBER: PRDescriberSettings = Field(
        default_factory=PRDescriberSettings, description="Pull request describer agent settings."
    )
    ISSUE_ADDRESSOR: IssueAddressorSettings = Field(
        default_factory=IssueAddressorSettings, description="Issue addressor agent settings."
    )
    REVIEW_ADDRESSOR: ReviewAddressorSettings = Field(
        default_factory=ReviewAddressorSettings, description="Review addressor agent settings."
    )
    PLAN_AND_EXECUTE: PlanAndExecuteSettings = Field(
        default_factory=PlanAndExecuteSettings, description="Plan and execute agent settings."
    )
    PIPELINE_FIXER: PipelineFixerSettings = Field(
        default_factory=PipelineFixerSettings, description="Pipeline fixer agent settings."
    )


settings = AutomationSettings()  # type: ignore
