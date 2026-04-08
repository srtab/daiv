from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DAIVAgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DAIV_AGENT_", env_parse_none_str="None")

    CUSTOM_SKILLS_PATH: Path | None = Field(
        default=Path.home() / "data" / "skills",
        description="Path to custom global skills directory. Set to None to disable.",
    )


settings = DAIVAgentSettings()
