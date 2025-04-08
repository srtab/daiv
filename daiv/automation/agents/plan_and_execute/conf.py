from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.constants import ModelName


class PlanAndExecuteSettings(BaseSettings):
    """
    Settings for the plan and execute agent.
    """

    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="PLAN_AND_EXECUTE_")

    NAME: str = Field(default="PlanAndExecute", description="Name of the plan and execute agent.")
    PLANNING_MODEL_NAME: ModelName | str = Field(
        default=ModelName.CLAUDE_3_7_SONNET, description="Model name to be used to plan tasks."
    )
    EXECUTION_MODEL_NAME: ModelName | str = Field(
        default=ModelName.CLAUDE_3_7_SONNET, description="Model name to be used to execute tasks."
    )
    PLAN_APPROVAL_MODEL_NAME: ModelName | str = Field(
        default=ModelName.GEMINI_2_0_FLASH, description="Model name to be used to evaluate the plan approval."
    )
    COMMAND_EXECUTION_ENABLED: bool = Field(default=True, description="Whether to enable command execution.")


settings = PlanAndExecuteSettings()  # type: ignore
