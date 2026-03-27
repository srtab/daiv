from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class JobsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JOBS_")

    THROTTLE_RATE: str = Field(default="20/hour", description="Rate limit for job submissions per authenticated user")


settings = JobsSettings()
