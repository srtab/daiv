from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.constants import ModelName


class ImageURLExtractorSettings(BaseSettings):
    """
    Settings for the image URL extractor agent.
    """

    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="IMAGE_URL_EXTRACTOR_")

    MODEL_NAME: ModelName = Field(
        default=ModelName.GPT_4O_MINI_2024_07_18, description="Model name to be used for image URL extractor."
    )


settings = ImageURLExtractorSettings()  # type: ignore
