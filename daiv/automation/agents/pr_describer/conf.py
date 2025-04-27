from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.constants import ModelName


class PRDescriberSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="PR_DESCRIBER_")

    NAME: str = Field(default="PullRequestDescriber", description="Name of the PR describer agent.")
    MODEL_NAME: ModelName | str = Field(
        default=ModelName.GPT_4_1_MINI, description="Model name to be used for PR describer."
    )


settings = PRDescriberSettings()  # type: ignore
