from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.base import ThinkingLevel
from automation.agents.constants import ModelName


class PlanAndExecuteSettings(BaseSettings):
    """
    Settings for the plan and execute agent.
    """

    model_config = SettingsConfigDict(env_prefix="PLAN_AND_EXECUTE_")

    NAME: str = Field(default="PlanAndExecute", description="Name of the plan and execute agent.")
    RECURSION_LIMIT: int = Field(default=100, description="Recursion limit for the plan and execute agent.")
    IMAGE_EXTRACTOR_MODEL_NAME: ModelName | str = Field(
        default=ModelName.GPT_4_1_NANO, description="Model name to be used to extract images from the task."
    )
    PLANNING_MODEL_NAME: ModelName | str = Field(
        default=ModelName.CLAUDE_SONNET_4, description="Model name to be used to plan tasks."
    )
    PLANNING_THINKING_LEVEL: ThinkingLevel | None = Field(
        default=ThinkingLevel.MEDIUM, description="Thinking level to be used for planning."
    )
    EXECUTION_MODEL_NAME: ModelName | str = Field(
        default=ModelName.CLAUDE_SONNET_4, description="Model name to be used to execute tasks."
    )


settings = PlanAndExecuteSettings()  # type: ignore
