from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class CoreSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="DAIV_")

    EXTERNAL_URL: HttpUrl = Field(default=HttpUrl("https://app:8000"), description="URL of the DAIV webapp")

    SANDBOX_URL: HttpUrl = Field(default=HttpUrl("http://sandbox:8000"), description="URL of the sandbox service")
    SANDBOX_TIMEOUT: float = Field(default=600, description="Timeout for sandbox requests in seconds")
    SANDBOX_API_KEY: str = Field(default="", description="API key for sandbox requests")


settings = CoreSettings()  # type: ignore
