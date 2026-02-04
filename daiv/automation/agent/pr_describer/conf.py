from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agent.constants import ModelName


class PRDescriberSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PR_DESCRIBER_", env_parse_none_str="None")

    MODEL_NAME: ModelName | str = Field(
        default=ModelName.GPT_4_1_MINI, description="Model name to be used for PR describer."
    )


settings = PRDescriberSettings()
