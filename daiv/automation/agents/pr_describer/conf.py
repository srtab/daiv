from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.constants import ModelName


class PRDescriberSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="PR_DESCRIBER_")

    NAME: str = Field(default="PullRequestDescriber", description="Name of the PR describer agent.")
    MODEL_NAME: ModelName = Field(
        default=ModelName.GEMINI_2_0_FLASH_LITE, description="Model name to be used for PR describer."
    )


settings = PRDescriberSettings()  # type: ignore
