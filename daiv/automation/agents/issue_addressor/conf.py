from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.constants import ModelName


class IssueAddressorSettings(BaseSettings):
    """
    Settings for the issue addressor agent.
    """

    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="ISSUE_ADDRESSOR_")

    NAME: str = Field(default="IssueAddressor", description="Name of the issue addressor agent.")
    RECURSION_LIMIT: int = Field(default=50, description="Recursion limit for the issue addressor agent.")
    ASSESSMENT_MODEL_NAME: ModelName | str = Field(
        default=ModelName.GPT_4_1_MINI, description="Model name to be used for issue assessment."
    )


settings = IssueAddressorSettings()  # type: ignore
