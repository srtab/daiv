from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from automation.agents.constants import ModelName


class ImageURLExtractorSettings(BaseSettings):
    """
    Settings for the image URL extractor agent.
    """

    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="IMAGE_URL_EXTRACTOR_")

    NAME: str = Field(default="ImageURLExtractor", description="Name of the image URL extractor agent.")
    MODEL_NAME: ModelName = Field(
        default=ModelName.GPT_4O_MINI, description="Model name to be used for image URL extractor."
    )


settings = ImageURLExtractorSettings()  # type: ignore
