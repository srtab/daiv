from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .base import GitPlatform


class CodebaseSettings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", env_prefix="CODEBASE_", env_parse_none_str="None")

    CLIENT: GitPlatform = Field(default=GitPlatform.GITLAB, description="Git platform to use for codebase operations")

    # GitLab
    GITLAB_URL: HttpUrl | None = Field(default=None, description="URL of the GitLab instance")
    GITLAB_AUTH_TOKEN: SecretStr | None = Field(default=None, description="Authentication token for GitLab")
    GITLAB_WEBHOOK_SECRET: SecretStr | None = Field(
        default=None, description="Secret token for GitLab webhook validation"
    )

    # GitHub
    GITHUB_URL: HttpUrl | None = Field(default=None, description="URL of the GitHub instance")
    GITHUB_APP_ID: int | None = Field(default=None, description="GitHub app ID")
    GITHUB_INSTALLATION_ID: int | None = Field(default=None, description="GitHub installation ID")
    GITHUB_PRIVATE_KEY: SecretStr | None = Field(default=None, description="GitHub private key")
    GITHUB_WEBHOOK_SECRET: SecretStr | None = Field(
        default=None, description="Secret token for GitHub webhook validation"
    )


settings = CodebaseSettings()  # type: ignore
