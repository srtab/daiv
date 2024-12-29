from typing import Literal

from pydantic import Field, HttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class CodebaseSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="CODEBASE_")

    CLIENT: Literal["gitlab"] = Field(default="gitlab", description="Client to use for codebase operations")

    # GitLab
    GITLAB_URL: HttpUrl | None = Field(default=None, description="URL of the GitLab instance")
    GITLAB_AUTH_TOKEN: str | None = Field(default=None, description="Authentication token for GitLab")

    # Pipeline fixer
    PIPELINE_FIXER_MAX_RETRY: int = Field(
        default=20, description="Maximum number of retry iterations for pipeline fixer"
    )


settings = CodebaseSettings()  # type: ignore
