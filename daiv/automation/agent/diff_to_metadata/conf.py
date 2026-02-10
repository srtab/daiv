from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agent.constants import ModelName


class DiffToMetadataSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DIFF_TO_METADATA_", env_parse_none_str="None")

    MODEL_NAME: ModelName | str = Field(
        default=ModelName.CLAUDE_HAIKU_4_5, description="Model name to be used to transform a diff into metadata."
    )
    FALLBACK_MODEL_NAME: ModelName | str = Field(
        default=ModelName.GPT_4_1_MINI, description="Fallback model name to be used when the primary model fails."
    )


settings = DiffToMetadataSettings()
