from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SessionsSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="DAIV_", env_parse_none_str="None")

    ARTIFACT_MAX_BYTES: int = Field(
        default=10 * 1024 * 1024,
        description="Maximum size, in bytes, of a single file the agent may publish as a run artifact.",
    )
    ARTIFACTS_PER_RUN_MAX: int = Field(
        default=20, description="Maximum number of artifacts a single agent run may publish."
    )


settings = SessionsSettings()
