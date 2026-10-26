from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class PluginsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLUGINS_", secrets_dir="/run/secrets")

    DATA_PATH: Path = Path.home() / "data" / "plugins"
    UPDATE_CRON: str = "0 * * * *"
    GIT_TIMEOUT_SECONDS: int = 120
    MAX_REPO_BYTES: int = 200 * 1024 * 1024
    MAX_PLUGIN_FILES: int = 1000
    MAX_PLUGIN_BYTES: int = 50 * 1024 * 1024
    MAX_FILE_BYTES: int = 5 * 1024 * 1024


settings = PluginsSettings()
