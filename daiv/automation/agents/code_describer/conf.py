from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.constants import ModelName


class CodeDescriberSettings(BaseSettings):
    """
    Settings for the code describer agent.
    """

    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="CODE_DESCRIBER_")

    NAME: str = Field(default="CodeDescriber", description="Name of the code describer agent.")
    MODEL_NAME: ModelName = Field(
        default=ModelName.GEMINI_2_0_FLASH_LITE, description="Model name to be used for code describer."
    )


settings = CodeDescriberSettings()  # type: ignore
