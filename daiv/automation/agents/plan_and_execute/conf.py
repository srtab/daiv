from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.base import ThinkingLevel  # noqa: TC001
from automation.agents.constants import ModelName


class PlanAndExecuteSettings(BaseSettings):
    """
    Settings for the plan and execute agent.
    """

    model_config = SettingsConfigDict(env_prefix="PLAN_AND_EXECUTE_")

    NAME: str = Field(default="PlanAndExecute", description="Name of the plan and execute agent.")
    PLANNING_RECURSION_LIMIT: int = Field(default=100, description="Recursion limit for the plan agent.")
    PLANNING_MODEL_NAME: ModelName | str = Field(
        default=ModelName.CLAUDE_SONNET_4_5,
        description="Model for planning tasks, a multi-modal (image and text) model with capabilities to call tools.",
    )
    PLANNING_FALLBACK_MODEL_NAME: ModelName | str = Field(
        default=ModelName.GPT_5_CODEX, description="Fallback model for planning tasks if the primary model fails."
    )
    PLANNING_THINKING_LEVEL: ThinkingLevel | None = Field(
        default=None, description="Thinking level to be used for planning. Set as `None` to disable thinking."
    )
    EXECUTION_RECURSION_LIMIT: int = Field(default=100, description="Recursion limit for the execute agent.")
    EXECUTION_MODEL_NAME: ModelName | str = Field(
        default=ModelName.CLAUDE_SONNET_4_5,
        description="Model to write code and run commands with capabilities to call tools.",
    )
    EXECUTION_FALLBACK_MODEL_NAME: ModelName | str = Field(
        default=ModelName.GPT_5_CODEX, description="Fallback model for execution tasks if the primary model fails."
    )
    CODE_REVIEW_MODEL_NAME: ModelName | str = Field(
        default=ModelName.GPT_5_MINI, description="Model to review code changes against the plan tasks ."
    )
    CODE_REVIEW_THINKING_LEVEL: ThinkingLevel | None = Field(
        default=ThinkingLevel.MEDIUM,
        description="Thinking level to be used for code review. Set as `None` to disable thinking.",
    )


settings = PlanAndExecuteSettings()
