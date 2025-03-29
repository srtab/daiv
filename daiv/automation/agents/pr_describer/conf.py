from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.constants import ModelName


class PRDescriberSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="PR_DESCRIBER_")

    NAME: str = Field(default="PullRequestDescriber", description="Name of the PR describer agent.")
    MODEL_NAME: ModelName = Field(
        default=ModelName.CLAUDE_3_5_HAIKU, description="Model name to be used for PR describer."
    )
    FALLBACK_MODEL_NAME: ModelName = Field(
        default=ModelName.GPT_4O_MINI, description="Fallback model name to be used for PR describer."
    )


settings = PRDescriberSettings()  # type: ignore
