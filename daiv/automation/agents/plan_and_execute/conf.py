from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.base import ThinkingLevel
from automation.agents.constants import ModelName


class PlanAndExecuteSettings(BaseSettings):
    """
    Settings for the plan and execute agent.
    """

    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="PLAN_AND_EXECUTE_")

    RECURSION_LIMIT: int = Field(default=50, description="Recursion limit for the plan and execute agent.")
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


settings = PlanAndExecuteSettings()  # type: ignore
