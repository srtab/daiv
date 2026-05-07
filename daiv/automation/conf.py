from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AutomationSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="AUTOMATION_", env_parse_none_str="None")

    WEB_FETCH_PROXY_URL: str | None = Field(default=None, description="Optional proxy URL for web_fetch HTTP requests.")


settings = AutomationSettings()
