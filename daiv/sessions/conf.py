from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SessionsSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="DAIV_", env_parse_none_str="None")

    ARTIFACT_MAX_BYTES: int = Field(
        default=10 * 1024 * 1024,
        gt=0,
        le=64 * 1024 * 1024,
        description="Maximum size, in bytes, of one published artifact; the sandbox download limit caps it at 64 MiB.",
    )
    ARTIFACTS_PER_RUN_MAX: int = Field(
        default=20, gt=0, description="Maximum number of artifacts a single agent run may publish."
    )


settings = SessionsSettings()
