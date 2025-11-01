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
    PLANNING_THINKING_LEVEL: ThinkingLevel | None = Field(
        default=None, description="Thinking level to be used for planning. Set as `None` to disable thinking."
    )
    EXECUTION_RECURSION_LIMIT: int = Field(default=100, description="Recursion limit for the execute agent.")
    EXECUTION_MODEL_NAME: ModelName | str = Field(
        default=ModelName.CLAUDE_SONNET_4_5,
        description="Model to write code and run commands with capabilities to call tools.",
    )


settings = PlanAndExecuteSettings()
