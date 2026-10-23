from typing import Literal

from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .base import GitPlatform


class CodebaseSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="CODEBASE_", env_parse_none_str="None")

    CLIENT: Literal[GitPlatform.GITLAB, GitPlatform.GITHUB] = Field(
        default=GitPlatform.GITLAB, description="Git platform to use for codebase operations"
    )

    # GitLab
    GITLAB_URL: HttpUrl | None = Field(default=None, description="URL of the GitLab instance")
    GITLAB_AUTH_TOKEN: SecretStr | None = Field(default=None, description="Authentication token for GitLab")

    REPO_ACCESS_SYNC_CRON: str = Field(
        default="*/15 * * * *", description="Cron expression for the periodic repository access sync"
    )
    REPO_ACCESS_HARD_TTL_HOURS: int = Field(
        default=24,
        description="Deny non-admin access to a repository when its synced access data is older than this many hours",
    )
    CROSS_PROJECT_RECORD_RETENTION_DAYS: int = Field(
        default=90, description="Days a cross-project access record is kept before the access sync prunes it"
    )

    # GitHub
    GITHUB_URL: HttpUrl | None = Field(default=None, description="URL of the GitHub instance")
    GITHUB_APP_ID: int | None = Field(default=None, description="GitHub app ID")
    GITHUB_INSTALLATION_ID: int | None = Field(default=None, description="GitHub installation ID")
    GITHUB_PRIVATE_KEY: SecretStr | None = Field(default=None, description="GitHub private key")


settings = CodebaseSettings()
